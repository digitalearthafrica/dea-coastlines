#!/usr/bin/env python
# coding: utf-8

# This code conducts vector subpixel shoreline extraction for DEA
# Coastlines:
#
#     * Apply morphological extraction algorithms to mask annual median
#       composite rasters to a valid coastal region
#     * Extract waterline vectors using subpixel waterline extraction
#       (Bishop-Taylor et al. 2019b; https://doi.org/10.3390/rs11242984)
#     * Compute rates of coastal change at every 30 m along Australia's
#       non-rocky coastlines using linear regression

import os
import sys
import glob
import click
import warnings
import numpy as np
import pandas as pd
import xarray as xr
import topojson as tp
import geopandas as gpd
from scipy import stats
from affine import Affine
from itertools import chain
from shapely.geometry import shape
from shapely.geometry import box
from shapely.geometry import LineString
from shapely.geometry import MultiLineString
from shapely.ops import nearest_points
from rasterio.features import shapes
from rasterio.features import sieve
from rasterio.features import rasterize
from rasterio.transform import array_bounds
from skimage.measure import label, regionprops
from skimage.measure import find_contours
from skimage.morphology import binary_opening
from skimage.morphology import binary_closing
from skimage.morphology import binary_erosion
from skimage.morphology import binary_dilation
from skimage.morphology import disk, square
from skimage.morphology import remove_small_objects
from datacube.utils.cog import write_cog

# Load dea-tools funcs
from dea_tools.spatial import subpixel_contours

# Import DEA Coastlines code
from dea_coastlines import raster

# Hide warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
pd.options.mode.chained_assignment = None


def load_rasters(path,
                 raster_version,
                 study_area,
                 water_index='mndwi',
                 start_year=1988):
    """
    Loads DEA Coastlines water index (e.g. 'MNDWI'), 'tide_m', 'count',
    and 'stdev' rasters for both annual and three-year gapfill data
    into a consistent `xarray.Dataset` format for further analysis.
    
    Parameters:
    -----------
    path : string
        A string giving the directory containing raster outputs.
    raster_version : string
        A string giving the unique DEA Coastlines analysis version
        (e.g. 'v0.3.0') used to load raster files.
    study_area : string or int
        A string giving the study area used to name raster files 
        (e.g. Albers tile `6931`).
    water_index : string, optional
        A string giving the name of the water index to load. Defaults
        to 'mndwi', which will load raster files produced using the
        Modified Normalised Difference Water Index.
    start_year : integer, optional
        The first annual layer to include in the analysis. Defaults to
        1988.

    Returns:
    --------
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA CoastLines rasters.
        The dataset contains water index (e.g. 'MNDWI'), 'tide_m', 
        'count', and 'stdev' arrays for each year from 1988 onward.
    gapfill_ds : xarray.Dataset
        An `xarray.Dataset` containing three-year gapfill DEA CoastLines
        rasters. The dataset contains water index (e.g. 'MNDWI'), 
        'tide_m', 'count', and 'stdev' arrays for each year from 1988 
        onward.
        
    """

    # List to hold output Datasets
    ds_list = []

    for layer_type in ['.tif', '_gapfill.tif']:

        # List to hold output DataArrays
        da_list = []

        for layer_name in [f'{water_index}', 'tide_m', 'count', 'stdev']:

            # Get paths of files that match pattern
            paths = glob.glob(f'{path}/{raster_version}/'
                              f'{study_area}_{raster_version}/'
                              f'*_{layer_name}{layer_type}')

            # Test if data was returned
            if len(paths) == 0:
                raise ValueError(
                    f"No rasters found for grid cell {study_area} "
                    f"(raster version '{raster_version}'). Verify that "
                    f"`raster.py` has been run "
                    "for this grid cell.")

            # Create variable used for time axis
            time_var = xr.Variable('year',
                                   [int(i.split('/')[-1][0:4]) for i in paths])

            # Import data
            layer_da = xr.concat([xr.open_rasterio(i) for i in paths],
                                 dim=time_var)
            layer_da.name = f'{layer_name}'

            # Append to file
            da_list.append(layer_da)

        # Combine into a single dataset and set CRS
        layer_ds = xr.merge(da_list).squeeze('band', drop=True)
        layer_ds = layer_ds.assign_attrs(layer_da.attrs)
        layer_ds.attrs['transform'] = Affine(*layer_ds.transform)
        layer_ds = layer_ds.sel(year=slice(start_year, None))

        # Append to list
        ds_list.append(layer_ds)

    return ds_list


def load_climate_data(index='soi_local',
                      years=(1988, 2020),
                      annual=True,
                      detrend=True):
    """
    Load climate index timeseries data from https://psl.noaa.gov,
    and optionally clip to a time range and aggregate by year.
    
    Parameters:
    -----------
    index : str
        A string giving the name of the climate index to load. 
        Currently supports 'soi', 'soi_local' (pointing to local
        path on NCI), and 'pdo'
    years : tuple of floats, optional
        A tuple giving the first and last year of data to include.
    annual : bool, optional
        Whether to aggregate data to annual means.
        
    Returns:
    --------
    climate_df : A pandas.DataFrame containing climate index timeseries. 
    """

    # Climate index dict
    index_urls = {
        'soi_local': {
            'url': '/data/raw/soi.long.data',
            'footer': 9,
            'nodata': -99.99
        },
        'soi': {
            'url':
                'https://psl.noaa.gov/gcos_wgsp/Timeseries/Data/soi.long.data',
            'footer':
                9,
            'nodata':
                -99.99
        },
        'pdo': {
            'url':
                'https://psl.noaa.gov/gcos_wgsp/Timeseries/Data/pdo.long.data',
            'footer':
                12,
            'nodata':
                -9.90
        }
    }

    # Load data
    climate_df = pd.read_csv(index_urls[index]['url'],
                             header=None,
                             delimiter=r'\s+',
                             skiprows=1,
                             index_col=0,
                             skipfooter=index_urls[index]['footer'],
                             engine='python',
                             na_values=index_urls[index]['nodata'])

    # Melt into long format
    climate_df.index.name = 'year'
    climate_df = climate_df.reset_index().melt(id_vars='year',
                                               var_name='month',
                                               value_name=index[:3])
    climate_df = climate_df.sort_values(['year', 'month'])

    # Annual means
    if annual:
        climate_df = climate_df.groupby('year').mean()

    # Clip to years
    if years:
        first_year, last_year = years
        climate_df = climate_df.query("@first_year <= year <= @last_year")

    # Detrend
    if detrend:
        index_regress = stats.linregress(x=climate_df.index,
                                         y=climate_df[index[:3]])
        climate_df.iloc[:, 0] = climate_df.iloc[:, 0] - (
            index_regress.slope * climate_df.index + index_regress.intercept)

    return climate_df


def waterbody_masking(input_data, modification_data, bbox, yearly_ds):
    """
    Generates a raster mask for DEACoastlines based on the 
    SurfaceHydrologyPolygonsRegional.gdb dataset, and a vector 
    file containing minor modifications to this dataset (e.g. 
    features to remove or add to the dataset).
    
    The mask returns True for perennial 'Lake' features, any 
    'Aquaculture Area', 'Estuary', 'Watercourse Area', 'Salt 
    Evaporator', and 'Settling Pond' features. Features of 
    type 'add' from the modification data file are added to the
    mask, while features of type 'remove' are removed.
    
    Parameters:
    -----------
    input_data : string
        A string giving the path to the file containing surface water
        polygons (e.g. SurfaceHydrologyPolygonsRegional.gdb)
    modification_data : string
        A string giving the path to a vector file containing 
        modifications to the waterbody file. This vector file should
        contain polygon features with an attribute field 'type'
        indicating whether the function should 'add' or 'remove' the 
        feature from the waterbody mask.
    bbox : geopandas.GeoSeries
        A `geopandas.GeoSeries` giving the spatial extent to load data 
        for. This object should include a CRS.
    yearly_ds : xr.Dataset
        The annual DEA CoastLines `xarray.Dataset`, used to extract the
        shape and geotransform so that waterbody features can be 
        rasterised into the data's extents.
        
    Returns:
    --------
    waterbody_mask : nd.array
        An array containing the rasterised surface water features.
    
    """

    # Import SurfaceHydrologyPolygonsRegional data
    waterbody_gdf = gpd.read_file(input_data, bbox=bbox).to_crs(yearly_ds.crs)

    # Restrict to coastal features
    lakes_bool = ((waterbody_gdf.FEATURETYPE == 'Lake') &
                  (waterbody_gdf.PERENNIALITY == 'Perennial'))
    other_bool = waterbody_gdf.FEATURETYPE.isin([
        'Aquaculture Area', 'Estuary', 'Watercourse Area', 'Salt Evaporator',
        'Settling Pond'
    ])
    waterbody_gdf = waterbody_gdf[lakes_bool | other_bool]

    # Load in modification dataset and select features to remove/add
    mod_gdf = gpd.read_file(modification_data, bbox=bbox).to_crs(yearly_ds.crs)
    to_remove = mod_gdf[mod_gdf['type'] == 'remove']
    to_add = mod_gdf[mod_gdf['type'] == 'add']

    # Remove and add features
    if len(to_remove.index) > 0:
        if len(waterbody_gdf.index) > 0:
            waterbody_gdf = gpd.overlay(waterbody_gdf,
                                        to_remove,
                                        how='difference')
    if len(to_add.index) > 0:
        if len(waterbody_gdf.index) > 0:
            waterbody_gdf = gpd.overlay(waterbody_gdf, to_add, how='union')
        else:
            waterbody_gdf = to_add

    # Rasterize waterbody polygons into a numpy mask. The try-except catches
    # cases where no waterbody polygons exist in the study area
    try:
        waterbody_mask = rasterize(shapes=waterbody_gdf['geometry'],
                                   out_shape=yearly_ds.geobox.shape,
                                   transform=yearly_ds.geobox.transform,
                                   all_touched=True).astype(bool)
    except:
        waterbody_mask = np.full(yearly_ds.geobox.shape, False, dtype=bool)

    return waterbody_mask


def ocean_masking(ds, tide_points_gdf, connectivity=1, dilation=None):
    """
    Identifies ocean by selecting the largest connected area of water
    pixels that contain tidal modelling points. This region can be
    optionally dilated to ensure that the sub-pixel algorithm has pixels 
    on either side of the water index threshold.
    
    Parameters:
    -----------
    ds : xarray.DataArray
        An array containing True for land pixels, and False for water.
        This can be obtained by thresholding a water index
        array (e.g. MNDWI < 0).
    tide_points_gdf : geopandas.GeoDataFrame
        Spatial points located within the ocean. These points are used
        to ensure that all coastlines are directly connected to the 
        ocean.
    connectivity : integer, optional
        An integer passed to the 'connectivity' parameter of the
        `skimage.measure.label` function.
    dilation : integer, optional
        The number of pixels to dilate ocean pixels to ensure than 
        adequate land pixels are included for subpixel waterline
        extraction. Defaults to None.
        
    Returns:
    --------
    ocean_mask : xarray.DataArray
        An array containing the a mask consisting of identified ocean 
        pixels as True.    
    """

    # First, break boolean array into unique, discrete regions/blobs
    blobs = xr.apply_ufunc(label, ds, 1, False, 1)

    # Get blob ID for each tidal modelling point
    x = xr.DataArray(tide_points_gdf.geometry.x, dims='z')
    y = xr.DataArray(tide_points_gdf.geometry.y, dims='z')
    ocean_blobs = np.unique(blobs.interp(x=x, y=y, method='nearest'))

    # Return only blobs that contained tide modelling point
    ocean_mask = blobs.isin(ocean_blobs[ocean_blobs != 0])

    # Dilate mask so that we include land pixels on the inland side
    # of each shoreline to ensure contour extraction accurately
    # seperates land and water spectra
    if dilation:
        ocean_mask = xr.apply_ufunc(binary_dilation,
                                    ocean_mask,
                                    disk(dilation))

    return ocean_mask


def coastal_masking(ds, tide_points_gdf, buffer=50, closing=None):
    """
    Creates a symmetrical buffer around the land-water boundary
    in a input boolean array. This is used to create a study area
    mask that is focused on the coastal zone, excluding inland or 
    deeper ocean pixels.
    
    Parameters:
    -----------
    ds : xarray.DataArray
        A single time-step boolean array containing True for land
        pixels, and False for water.
    tide_points_gdf : geopandas.GeoDataFrame
        Spatial points located within the ocean. These points are used
        to ensure that all coastlines are directly connected to the 
        ocean.
    buffer : integer, optional
        The number of pixels to buffer the land-water boundary in
        each direction. 
        
    Returns:
    --------
    coastal_mask : xarray.DataArray
        An array containing True within `buffer_pixels` of the
        land-water boundary, and False everywhere else.    
    """

    def _coastal_buffer(ds, buffer):
        """Generate coastal buffer from ocean-land boundary"""
        buffer_ocean = binary_dilation(ds, buffer)
        buffer_land = binary_dilation(~ds, buffer)
        return buffer_ocean & buffer_land
    
    # If closing is specified, apply morphological closing to fill
    # narrow rivers, excluding them from the output study area
    if closing:
        ds = xr.apply_ufunc(binary_closing, ds, disk(5))

    # Identify ocean pixels that are directly connected to tide points
    all_time_ocean = ocean_masking(ds, tide_points_gdf)

    # Generate coastal buffer from ocean-land boundary
    coastal_mask = xr.apply_ufunc(_coastal_buffer,
                                  all_time_ocean,
                                  disk(buffer),
                                  dask='parallelized')

    return coastal_mask


def temporal_masking(ds):
    """
    Create a temporal mask by identifying land pixels with a direct 
    spatial connection (e.g. contiguous) to land pixels in either the 
    previous or subsequent timestep. 
    
    This is used to clean up noisy land pixels (e.g. caused by clouds,
    white water, sensor issues), as these pixels typically occur 
    randomly with no relationship to the distribution of land in 
    neighbouring timesteps. True land, however, is likely to appear 
    in proximity to land before or after the specific timestep.
    
    Parameters:
    -----------
    ds : xarray.DataArray
        A multi-temporal array containing True for land pixels, and 
        False for water.
        
    Returns:
    --------
    temporal_mask : xarray.DataArray
        A multi-temporal array array containing True for pixels 
        located within the `dilation` distance of land in at least 
        one neighbouring timestep.
    """

    def _noncontiguous(labels, intensity):

        # For each blob of land, obtain whether it intersected with land in
        # any neighbouring timestep
        region_props = regionprops(labels.values,
                                   intensity_image=intensity.values)
        contiguous = [i.label for i in region_props if i.max_intensity == 0]

        # Filter array to only contiguous land
        noncontiguous_array = np.isin(labels, contiguous)

        # Return as xr.DataArray
        return xr.DataArray(~noncontiguous_array,
                            coords=labels.coords,
                            dims=labels.dims)

    # Label independent groups of pixels in each timestep in the array
    labelled_ds = xr.apply_ufunc(label, ds, None, 0,
                                 dask='parallelized').rename('labels')

    # Check if a pixel was neighboured by land in either the
    # previous or subsequent timestep by shifting array in both directions
    masked_neighbours = (ds.shift(year=-1, fill_value=False) | ds.shift(
        year=1, fill_value=False)).astype(int).rename('neighbours')

    # Merge both into an xr.Dataset
    label_neighbour_ds = xr.merge([labelled_ds, masked_neighbours])

    # Apply continguity test to each year to obtain pixels that are
    # contiguous (spatially connected to) to land in the previous or subsequent timestep
    temporal_mask = label_neighbour_ds.groupby('year').apply(
        lambda x: _noncontiguous(labels=x.labels, intensity=x.neighbours))

    return temporal_mask


def contours_preprocess(yearly_ds,
                        gapfill_ds,
                        water_index,
                        index_threshold,
                        waterbody_mask,
                        tide_points_gdf,
                        output_path,
                        buffer_pixels=50):
    """
    Prepares and preprocesses DEA Coastlines raster data to restrict the
    analysis to coastal shorelines, and extract data that is used to
    assess the certainty of extracted shorelines.
    
    This function:
    
    1) Identifies areas affected by either tidal issues, or low data
    2) Fills low data areas in annual layers with three-year gapfill
    3) Masks data to focus on ocean and coastal pixels only by removing
       any pixels not directly connected to ocean or included in an
       array of surface water (e.g. estuaries or inland waterbodies)
    4) Generate an overall coastal buffer using the entire timeseries,
       and clip each annual layer to this buffer
    5) Generate an all time mask raster containing data on tidal issues, 
       low data and coastal buffer to assist in interpreting results.
    
    Parameters:
    -----------
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA Coastlines rasters.
    gapfill_ds : xarray.Dataset
        An `xarray.Dataset` containing three-year gapfill DEA Coastlines
        rasters. 
    water_index : string
        A string giving the name of the water index included in the 
        annual and gapfill datasets (e.g. 'mndwi').
    index_threshold : float
        A float giving the water index threshold used to separate land
        and water (e.g. 0.00).
    waterbody_array : nd.array
        An array containing rasterised surface water features to exclude
        from the data, used by the `mask_ocean` function.
    tide_points_gdf : geopandas.GeoDataFrame
        Spatial points located within the ocean. These points are used
        by the `mask_ocean` to ensure that all coastlines are directly 
        connected to the ocean. These may be obtained from the tidal 
        modelling points used in the raster generation part of the DEA 
        CoastLines analysis, as these are guaranteed to be located in 
        coastal or marine waters.
    output_path : string
        A string giving the directory into which output all time mask 
        raster will be written.
    buffer_pixels : int, optional
        The number of pixels by which to buffer the all time shoreline
        detected by this function to produce an overall coastal buffer.
        The default is 50 pixels, which at 30 m Landsat resolution 
        produces a coastal buffer with a radius of approximately 1500 m.
        
    Returns:
    --------
    masked_ds : xarray.Dataset
        A dataset containing water index data for each annual timestep
        that has been masked to the coastal zone. This can then be used
        as an input to subpixel waterline extraction.
    
    """

    # Flag nodata pixels
    nodata = yearly_ds[water_index].isnull()

    # Identify pixels with less than 5 annual observations or > 0.25
    # MNDWI standard deviation in more than half the time series.
    # Apply binary erosion to isolate large connected areas of
    # problematic pixels
    mean_stdev = (yearly_ds['stdev'] > 0.25).where(~nodata).mean(dim='year')
    mean_count = (yearly_ds['count'] < 5).where(~nodata).mean(dim='year')
    persistent_stdev = binary_erosion(mean_stdev > 0.5, selem=disk(2))
    persistent_lowobs = binary_erosion(mean_count > 0.5, selem=disk(2))

    # Remove low obs pixels and replace with 3-year gapfill
    yearly_ds = yearly_ds.where(yearly_ds['count'] > 5, gapfill_ds)

    # Update nodata layer based on gap-filled data and waterbody array
    nodata = yearly_ds[water_index].isnull() | waterbody_mask

    # Apply water index threshold
    thresholded_ds = (yearly_ds[water_index] < index_threshold)
    
    import odc.algo
    import datacube
    dc = datacube.Datacube()
    landcover = dc.load(product='esa_worldcover', like=yearly_ds.geobox)
    landcover_water = landcover.classification.isin([0, 80]).squeeze(dim='time')
    landcover_water = odc.algo.mask_cleanup(landcover_water, mask_filters=[('erosion', 20)])
    thresholded_ds = thresholded_ds.where(~landcover_water, False).where(~nodata)
    
    # Compute temporal mask
    temporal_mask = temporal_masking(thresholded_ds == 1)

    # Identify pixels that are land in at least 20% of observations,
    # and use this to generate a coastal buffer
    all_time = ((thresholded_ds != 0) & temporal_mask).mean(dim='year') >= 0.2
    coastal_mask = coastal_masking(ds=all_time, 
                                   tide_points_gdf=tide_points_gdf, 
                                   buffer=buffer_pixels,
                                   closing=5)

    # Generate annual masks by selecting only water pixels that are
    # directly connected to the ocean in each yearly timestep
    #
    # TODO: apply coastal mask before this step to exclude non-contiguous
    # sections of shoreline being included in outputs. First attempt at
    # applying this didn't work, as masking by coastal mask before this 
    # step fails when there are no tidal points in the coastal mask to 
    # identify ocean vs. non-ocean using `ocean_masking`
    annual_mask = ((thresholded_ds != 0)
                   .groupby('year')
                   .apply(lambda x: ocean_masking(x, 
                                                  tide_points_gdf, 
                                                  1, 3)))    

    # Keep pixels within both all time coastal buffer, annual mask 
    # and temporal mask
    masked_ds = yearly_ds[water_index].where(annual_mask & 
                                             coastal_mask &
                                             temporal_mask)

    # Create raster containg all time mask data
    all_time_mask = np.full(yearly_ds.geobox.shape, 0, dtype='int8')
    all_time_mask[~coastal_mask] = 1
    all_time_mask[persistent_stdev & coastal_mask] = 4
    all_time_mask[persistent_lowobs & coastal_mask] = 5
    all_time_mask[waterbody_mask & coastal_mask] = 3

    # Export mask raster to assist evaluating results
    all_time_mask_da = xr.DataArray(data=all_time_mask,
                                    coords={
                                        'x': yearly_ds.x,
                                        'y': yearly_ds.y
                                    },
                                    dims=['y', 'x'],
                                    name='all_time_mask',
                                    attrs=yearly_ds.attrs)
    write_cog(geo_im=all_time_mask_da,
              fname=f'{output_path}/all_time_mask.tif',
              blocksize=256,
              overwrite=True)

    # Reset attributes and return data
    masked_ds.attrs = yearly_ds.attrs

    return masked_ds


def points_on_line(gdf, index, distance=30):
    """
    Generates evenly-spaced point features along a specific line feature
    in a `geopandas.GeoDataFrame`.
    
    Parameters:
    -----------
    gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing line features with an 
        index and CRS.
    index : string or int
        An value giving the index of the line to generate points along
    distance : integer or float, optional
        A number giving the interval at which to generate points along 
        the line feature. Defaults to 30, which will generate a point
        at every 30 metres along the line.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing point features at every
        `distance` along the selected line.
    
    """

    # Select individual line to generate points along
    line_feature = gdf.loc[[index]].geometry

    # If multiple features are returned, take unary union
    if line_feature.shape[0] > 0:
        line_feature = line_feature.unary_union
    else:
        line_feature = line_feature.iloc[0]

    # Generate points along line and convert to geopandas.GeoDataFrame
    points_line = [
        line_feature.interpolate(i)
        for i in range(0, int(line_feature.length), distance)
    ]
    points_gdf = gpd.GeoDataFrame(geometry=points_line, crs=gdf.crs)

    return points_gdf


def rocky_shores_clip(points_gdf, smartline_gdf, buffer=50):
    """
    Clips rates of change points to a buffer around non-rocky (clastic)
    coastlines based on the Smartline dataset.
    
    This processing step aims to be conservative, and preserves any
    unclassified points or any points that occur next to a non-rocky 
    shoreline in either the 'INTERTD1_V' or 'INTERTD2_V' Smartline 
    fields.    
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points.
    smartline_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing SmartLine data for the
        study area.
    buffer : integer or float, optional
        A number giving the buffer around non-rocky (clastic) shorelines
        within which to clip rates of change points. Defaults to 50 m.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        restricted to non-rocky (clastic) coastlines.    
    """

    rocky = [
        'Bedrock breakdown debris (cobbles/boulders)', 'Boulder (rock) beach',
        'Cliff (>5m) (undiff)', 'Colluvium (talus) undiff',
        'Flat boulder deposit (rock) undiff', 'Hard bedrock shore',
        'Hard bedrock shore inferred', 'Hard rock cliff (>5m)',
        'Hard rocky shore platform', 'Rocky shore (undiff)',
        'Rocky shore platform (undiff)', 'Sloping hard rock shore',
        'Sloping rocky shore (undiff)', 'Soft `bedrock¿ cliff (>5m)',
        'Steep boulder talus', 'Hard rocky shore platform'
    ]

    # Identify rocky features
    rocky_bool = (smartline_gdf.INTERTD1_V.isin(rocky) &
                  smartline_gdf.INTERTD2_V.isin(rocky + ['Unclassified']))

    # Extract rocky vs non-rocky
    rocky_gdf = smartline_gdf[rocky_bool].copy()
    nonrocky_gdf = smartline_gdf[~rocky_bool].copy()

    # If both rocky and non-rocky shorelines exist, clip points to remove
    # rocky shorelines from the stats dataset
    if (len(rocky_gdf) > 0) & (len(nonrocky_gdf) > 0):

        # Buffer both features
        rocky_gdf['geometry'] = rocky_gdf.buffer(buffer)
        nonrocky_gdf['geometry'] = nonrocky_gdf.buffer(buffer)
        rocky_shore_buffer = (gpd.overlay(
            rocky_gdf, nonrocky_gdf, how='difference').geometry.unary_union)

        # Keep only non-rocky shore features and reset index
        points_gdf = points_gdf[~points_gdf.intersects(rocky_shore_buffer)]
        points_gdf = points_gdf.reset_index(drop=True)

        return points_gdf

    # If no rocky shorelines exist, return the points data as-is
    elif len(nonrocky_gdf) > 0:
        return points_gdf

    # If no sandy shorelines exist, return nothing
    else:
        return None


def annual_movements(points_gdf,
                     contours_gdf,
                     yearly_ds,
                     baseline_year,
                     water_index,
                     max_valid_dist=1000):
    """
    For each rate of change point along the baseline annual coastline, 
    compute the distance to the nearest point on all neighbouring annual
    coastlines and add this data as new fields in the dataset.
    
    Distances are assigned a directionality (negative = located inland, 
    positive = located sea-ward) by sampling water index values from the 
    underlying DEA Coastlines rasters to determine if a coastline was 
    located in wetter or drier terrain than the baseline coastline.
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points.
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines.
    yearly_ds : xarray.Dataset
        An `xarray.Dataset` containing annual DEA CoastLines rasters.
    baseline_year : string
        A string giving the year used as the baseline when generating 
        the rates of change points dataset. This is used to load DEA
        CoastLines water index rasters to calculate change 
        directionality.
    water_index : string
        A string giving the water index used in the analysis. This is 
        used to load DEA CoastLines water index rasters to calculate 
        change directionality.
    max_valid_dist : int or float, optional
        Any annual distance greater than this distance will be set
        to `np.nan`.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        with added 'dist_*' attribute columns giving the distance to
        each annual coastline from the baseline. Negative values
        indicate that an annual coastline was located inland of the 
        baseline; positive values indicate the coastline was located 
        towards the ocean.
    """

    # Get array of water index values for baseline time period
    baseline_array = yearly_ds[water_index].sel(year=int(baseline_year))

    # Copy baseline point geometry to new column in points dataset
    points_gdf['p_baseline'] = points_gdf.geometry
    baseline_x_vals = points_gdf.geometry.x
    baseline_y_vals = points_gdf.geometry.y

    # Years to analyse
    years = contours_gdf.index.unique().values

    # Iterate through all comparison years in contour gdf
    for comp_year in years:

        print(comp_year, end='\r')

        # Set comparison contour
        comp_contour = contours_gdf.loc[[comp_year]].geometry.iloc[0]

        # Find nearest point on comparison contour, and add these to points dataset
        points_gdf[f'p_{comp_year}'] = points_gdf.apply(
            lambda x: nearest_points(x.p_baseline, comp_contour)[1], axis=1)

        # Compute distance between baseline and comparison year points and add
        # this distance as a new field named by the current year being analysed
        distances = points_gdf.apply(
            lambda x: x.geometry.distance(x[f'p_{comp_year}']), axis=1)

        # Set any value over X m to NaN, and drop any points with
        # less than 50% valid observations
        points_gdf[f'dist_{comp_year}'] = distances.where(
            distances < max_valid_dist)

        # Extract comparison array containing water index values for the
        # current year being analysed
        comp_array = yearly_ds[water_index].sel(year=int(comp_year))

        # Convert baseline and comparison year points to geoseries to allow
        # easy access to x and y coords
        comp_x_vals = gpd.GeoSeries(points_gdf[f'p_{comp_year}']).x
        comp_y_vals = gpd.GeoSeries(points_gdf[f'p_{comp_year}']).y

        # Sample water index values from arrays for baseline and comparison points
        baseline_x_vals = xr.DataArray(baseline_x_vals, dims='z')
        baseline_y_vals = xr.DataArray(baseline_y_vals, dims='z')
        comp_x_vals = xr.DataArray(comp_x_vals, dims='z')
        comp_y_vals = xr.DataArray(comp_y_vals, dims='z')
        points_gdf['index_comp_p1'] = comp_array.interp(x=baseline_x_vals,
                                                        y=baseline_y_vals)
        points_gdf['index_baseline_p2'] = baseline_array.interp(x=comp_x_vals,
                                                                y=comp_y_vals)

        # Compute change directionality (negative = located inland,
        # positive = located towards the ocean)
        points_gdf['loss_gain'] = np.where(
            points_gdf.index_baseline_p2 > points_gdf.index_comp_p1, 1, -1)
        points_gdf[f'dist_{comp_year}'] = (points_gdf[f'dist_{comp_year}'] *
                                           points_gdf.loss_gain)

    # Keep required columns
    to_keep = points_gdf.columns.str.contains('dist|geometry')
    points_gdf = points_gdf.loc[:, to_keep]
    points_gdf = points_gdf.assign(**{f'dist_{baseline_year}': 0.0})
    points_gdf = points_gdf.round(2)

    return points_gdf


def outlier_mad(points, thresh=3.5):
    """
    Use robust Median Absolute Deviation (MAD) outlier detection 
    algorithm to detect outliers. Returns a boolean array with True if 
    points are outliers and False otherwise.

    Parameters:
    -----------
    points : 
        An n-observations by n-dimensions array of observations
    thresh : 
        The modified z-score to use as a threshold. Observations with a 
        modified z-score (based on the median absolute deviation) greater
        than this value will be classified as outliers.

    Returns:
    --------
    mask : 
        A n-observations-length boolean array.

    References:
    ----------
    Source: https://github.com/joferkington/oost_paper_code/blob/master/utilities.py
    
    Boris Iglewicz and David Hoaglin (1993), "Volume 16: How to Detect and
    Handle Outliers", The ASQC Basic References in Quality Control:
    Statistical Techniques, Edward F. Mykytka, Ph.D., Editor. 
    """
    if len(points.shape) == 1:
        points = points[:, None]
    median = np.median(points, axis=0)
    diff = np.sum((points - median)**2, axis=-1)
    diff = np.sqrt(diff)
    med_abs_deviation = np.median(diff)

    modified_z_score = 0.6745 * diff / med_abs_deviation

    return modified_z_score > thresh


def change_regress(y_vals,
                   x_vals,
                   x_labels,
                   threshold=3.5,
                   detrend_params=None,
                   slope_var='slope',
                   interc_var='intercept',
                   pvalue_var='pvalue',
                   stderr_var='stderr',
                   outliers_var='outliers'):
    """
    For a given row in a `pandas.DataFrame`, apply linear regression to
    data values (as y-values) and a corresponding sequence of x-values, 
    and return 'slope', 'intercept', 'pvalue', and 'stderr' regression
    parameters.
    
    Before computing the regression, outliers are identified using a
    robust Median Absolute Deviation (MAD) outlier detection algorithm,
    and excluded from the regression. A list of these outliers will be
    recorded in the output 'outliers' variable.

    Parameters:
    -----------
    x_vals, y_vals : list of numeric values, or nd.array
        A sequence of values to use as the x and y variables
    x_labels : list
        A sequence of strings corresponding to each value in `x_vals`.
        This is used to label any observations that are flagged as 
        outliers (often, this can simply be set to the same list 
        provided to `x_vals`).
    threshold : float, optional    
        The modified z-score to use as a threshold for detecting 
        outliers using the MAD algorithm. Observations with a modified 
        z-score (based on the median absolute deviation) greater
        than this value will be classified as outliers.
    detrend_params : optional
        Not currently used
    slope, interc_var, pvalue_var, stderr_var : strings, optional
        Strings giving the names to use for each of the output 
        regression variables.    
    outliers_var : string, optional
        String giving the name to use for the output outlier variable.        

    Returns:
    --------
    mask : 
        A `pandas.Series` containing regression parameters and lists
        of outliers.
    
    """

    # Drop invalid NaN rows
    xy_df = np.vstack([x_vals, y_vals]).T
    valid_bool = ~np.isnan(xy_df).any(axis=1)
    xy_df = xy_df[valid_bool]
    valid_labels = x_labels[valid_bool]

    # If detrending parameters are provided, apply these to the data to
    # remove the trend prior to running the regression
    if detrend_params:
        xy_df[:, 1] = xy_df[:, 1] - (detrend_params[0] * xy_df[:, 0] +
                                     detrend_params[1])

    # Remove outliers using MAD
    outlier_bool = outlier_mad(xy_df, thresh=threshold)
    xy_df = xy_df[~outlier_bool]
    valid_labels = valid_labels[~outlier_bool]

    # Create string of all outliers and invalid NaN rows
    outlier_set = set(x_labels) - set(valid_labels)
    outlier_str = ' '.join(map(str, sorted(outlier_set)))

    # Compute linear regression
    lin_reg = stats.linregress(x=xy_df[:, 0], y=xy_df[:, 1])

    # Return slope, p-values and list of outlier years excluded from regression
    results_dict = {
        slope_var: np.round(lin_reg.slope, 3),
        interc_var: np.round(lin_reg.intercept, 3),
        pvalue_var: np.round(lin_reg.pvalue, 3),
        stderr_var: np.round(lin_reg.stderr, 3),
        outliers_var: outlier_str
    }

    return pd.Series(results_dict)


def calculate_regressions(points_gdf, contours_gdf, climate_df):
    """
    For each rate of change point along the baseline annual coastline, 
    compute linear regression rates of change against both time and
    climate indices.
    
    Regressions are computed after removing outliers to ensure robust
    results.
    
    Parameters:
    -----------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points 
        with 'dist_*' annual movement/distance data.
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines. This
        is used to ensure that all years in the annual coastlines data
        are included in the regression.
    climate_df : pandas.DataFrame
        A dataframe including numeric climate index data for each year
        in the input `contours_gdf` dataset.
        
    Returns:
    --------
    points_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing rates of change points
        with additional attribute columns:
        
            'rate_*':  Slope of the regression 
            'sig_*':   Significance of the regression
            'se_*':    Standard error of the  regression
            'outl_*':  A list of any outlier years excluded from the 
                       regression        
    """

    # Restrict climate and points data to years in datasets
    x_years = contours_gdf.index.unique().astype(int).values
    dist_years = [f'dist_{i}' for i in x_years]
    points_subset = points_gdf[dist_years]
    climate_subset = climate_df.loc[x_years, :]

    # Compute coastal change rates by linearly regressing annual
    # movements vs. time
    print(f'Comparing annual movements with time')
    rate_out = (points_subset.apply(lambda row: change_regress(
        y_vals=row.values.astype(float), x_vals=x_years, x_labels=x_years),
                                    axis=1))
    points_gdf[['rate_time', 'incpt_time', 'sig_time', 'se_time',
                'outl_time']] = rate_out

    # Copy slope and intercept into points_subset so they can be
    # used to temporally de-trend annual distances
    points_subset[['slope', 'intercept']] = rate_out[['slope', 'intercept']]

    # Identify possible relationships between climate indices and
    # coastal change by linearly regressing climate indices against
    # annual movements. Significant results indicate that annual
    # movements may be influenced by climate phenomena
    for ci in climate_subset:

        print(f'Comparing annual movements with {ci}')

        # Compute stats for each row
        ci_out = (points_subset.apply(lambda row: change_regress(
            y_vals=row.values[:-2].astype(float),
            x_vals=climate_subset[ci].values,
            x_labels=x_years,
            detrend_params=(row.slope, row.intercept)),
                                      axis=1))

        # Add data as columns
        points_gdf[[
            f'rate_{ci}', f'incpt_{ci}', f'sig_{ci}', f'se_{ci}', f'outl_{ci}'
        ]] = ci_out

    # Set CRS
    points_gdf.crs = contours_gdf.crs

    # Custom sorting
    reg_cols = chain.from_iterable(
        [f'rate_{i}', f'sig_{i}', f'se_{i}', f'outl_{i}']
        for i in ['time', *climate_df.columns])

    return points_gdf.loc[:, [*reg_cols, *dist_years, 'geometry']]


def all_time_stats(x, col='dist_', initial_year=1988):
    """
    Apply any statistics that apply to the entire set of annual 
    distance/movement values. This currently includes:
    
        valid_obs, valid_span : The number of valid (non-outlier) 
             obervations, and the length of time in years between
             the first and last valid observation.
        sce: Shoreline Change Envelope (SCE). A measure of the maximum 
             change or variability across all annual coastlines, 
             calculated by computing the maximum distance between any 
             two annual coastlines (excluding outliers).
        nsm: Net Shoreline Movement (NSM). The distance between the
             oldest and most recent annual shorelines (excluding
             outliers). Negative values indicate the shoreline retreated
             between the oldest and most recent shoreline; positive
             values indicate growth.
        max_year, min_year: The year that annual shorelines were at
             their maximum (i.e. located furthest towards the ocean) and
             their minimum (i.e. located furthest inland) respectively
             (excluding outliers). 
    
    Parameters:
    -----------
    x : pandas.DataFrame row
        A single row of the annual rates of change `pandas.DataFrame`
        containg columns of annual distances from the baseline.
    col : string, optional
        A string giving the prefix used for all annual distance/
        movement values. The default is 'dist_'.
    initial_year : int, optional
        An optional integer giving the first year of data to use when
        calculating statistics. This can be useful when data from early
        in the satellite timeseries is less reliable than more recent
        data, e.g. in regions with sparse Landsat 5 satellite coverage.
        
    Returns:
    --------
    A `pandas.Series` containing new all time statistics.
    """

    # Select date columns only
    year_cols = x.index.str.contains(col)
    subset = x.loc[year_cols].astype(float)

    # Restrict to requested initial year
    subset.index = subset.index.str.lstrip('dist_').astype(int)
    subset = subset.loc[initial_year:]

    # Identify outlier years to drop from calculation
    to_drop = [int(i) for i in x.outl_time.split(" ") if len(i) > 0]
    subset_nooutl = subset.drop(to_drop, errors='ignore')

    # Calculate SCE range, NSM and max/min year
    # Since NSM is the most recent shoreline minus the oldest shoreline,
    # we can calculate this by simply inverting the 1988 distance value
    # (i.e. 0 - X) if it exists in the data
    stats_dict = {
        'valid_obs':
            subset_nooutl.shape[0],
        'valid_span': (subset_nooutl.index[-1] - subset_nooutl.index[0] + 1),
        'sce':
            subset_nooutl.max() - subset_nooutl.min(),
        'nsm':
            -(subset_nooutl.loc[initial_year]
              if initial_year in subset_nooutl else np.nan),
        'max_year':
            subset_nooutl.idxmax(),
        'min_year':
            subset_nooutl.idxmin()
    }

    return pd.Series(stats_dict)


def contour_certainty(contours_gdf, output_path, uncertain_classes=[4, 5]):
    """
    Assigns a new certainty column to each annual shoreline feature
    based on two factors:
    
    1) Low satellite observations: pixels with less than 5 annual 
       observations for more than half of the time series.
    2) Tidal modelling issues: MNDWI standard deviation > 0.25 in more 
       than half of the time series.
    3) 1991 and 1992 coastlines affected by aerosol issues associated 
       with the 1991 eruption of Mt Pinatubo
    
    Parameters:
    -----------
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` containing annual coastlines. This
        is used to ensure that all years in the annual coastlines data
        are included in the regression.
    output_path : string
        A string giving the directory where the 'all_time_mask.tif' file 
        was generated by the `contours_preprocess` function.
    uncertain_classes : list, optional
        A list of integers giving the classes in the 'all_time_mask.tif'
        to treat as uncertain (e.g. low satellite observations and tidal
        modelling issues).
        
    Returns:
    --------
    contours_gdf : geopandas.GeoDataFrame
        A `geopandas.GeoDataFrame` of annual coastlines with a new 
        'certainty' column.
    """

    def _extract_multiline(row):

        if row.geometry.type == 'GeometryCollection':
            lines = [g for g in row.geometry.geoms if g.type == 'LineString']
            return MultiLineString(lines)
        else:
            return row.geometry

    # Read data and restrict to uncertain vs certain classes
    all_time_mask = xr.open_rasterio(f'{output_path}/all_time_mask.tif')
    uncertain_array = all_time_mask.squeeze().drop('band').data.astype(np.int32)
    uncertain_array[~np.isin(uncertain_array, uncertain_classes)] = 0

    # Remove isolated pixels and vectorise data
    uncertain_array = sieve(uncertain_array, size=3)
    vectors = shapes(source=uncertain_array,
                     transform=all_time_mask.geobox.transform)

    # Extract the polygon coordinates and values from the list
    vectors = list(vectors)
    polygons = [shape(polygon) for polygon, value in vectors]
    values = [int(value) for polygon, value in vectors]

    # Create a geopandas dataframe populated with the polygon shapes
    vector_mask = gpd.GeoDataFrame(data={'certainty': values},
                                   geometry=polygons,
                                   crs=all_time_mask.geobox.crs)

    # Dissolve by class and simplify features to remove hard pixel edges
    topo = tp.Topology(vector_mask, shared_coords=True, prequantize=False)
    vector_mask = topo.toposimplify(30).to_gdf()
    vector_mask = vector_mask.dissolve('certainty')
    vector_mask['geometry'] = vector_mask.geometry.buffer(0)

    # Rename classes
    vector_mask = vector_mask.rename({
        0: 'good',
        4: 'tidal issues',
        5: 'insufficient data'
    })

    # Output class list
    class_list = []

    # Iterate through each certainty class in the polygon, clip contours
    # to the extent of this class, and assign descriptive class name
    for i in vector_mask.index:

        # Clip to extent and fix invalid GeometryCollections
        vector_class = gpd.clip(contours_gdf, vector_mask.loc[i].geometry)
        vector_class = vector_class.dropna()

        if len(vector_class.index) > 0:
            vector_class['geometry'] = gpd.GeoSeries(
                vector_class.apply(_extract_multiline, axis=1))

            # Give name and append to list
            vector_class['certainty'] = i
            class_list.append(vector_class)

    # Combine into a single dataframe
    contours_gdf = pd.concat(class_list)

    # Finally, set all 1991 and 1992 coastlines north of -23 degrees
    # latitude to 'uncertain' due to Mt Pinatubo aerosol issue
    pinatubo_lat = ((contours_gdf.centroid.to_crs('EPSG:4326').y > -23) &
                    (contours_gdf.index.isin(['1991', '1992'])))
    contours_gdf.loc[pinatubo_lat, 'certainty'] = 'aerosol issues'

    return contours_gdf


@click.command()
@click.option('--config_path',
              type=str,
              required=True,
              help='Path to the YAML config file defining inputs to '
              'use for this analysis. These are typically located in '
              'the `dea-coastlines/configs/` directory.')
@click.option('--study_area',
              type=str,
              required=True,
              help='A string providing a unique ID of an analysis '
              'gridcell that was previously used to generate raster '
              'files. This is used to identify the raster files that '
              'will be used as inputs for shoreline extraction, and '
              'should match a row in the "id" column of the provided '
              'analysis gridcell vector file.')
@click.option('--raster_version',
              type=str,
              required=True,
              help='A unique string providing a name that was used '
              'to generate raster files. This is used to identify the '
              'raster files that will be used as inputs for shoreline '
              'extraction.')
@click.option('--vector_version',
              type=str,
              help='A unique string proving a name that will be used '
              'for output vector directories and files. This allows '
              'multiple versions of vector files to be generated '
              'from the same input raster data, e.g. for testing '
              'different water index thresholds or indices. If '
              'not provided, this will default to the same string '
              'supplied to "--raster_version".')
@click.option('--water_index',
              type=str,
              default='mndwi',
              help='A string giving the name of the computed water '
              'index to use for shoreline extraction. '
              'Defaults to "mndwi".')
@click.option('--index_threshold',
              type=float,
              default=0.00,
              help='The water index threshold used to extract '
              'subpixel precision shorelines. Defaults to 0.00.')
@click.option('--baseline_year',
              type=str,
              default='2020',
              help='The annual shoreline used to generate the '
              'rates of change point statistics. This is typically '
              'the most recent annual shoreline in the dataset.')
def generate_vectors(config_path, study_area, raster_version, vector_version,
                     water_index, index_threshold, baseline_year):

    ###############################
    # Load DEA Coastlines rasters #
    ###############################

    # Load analysis params from config file
    config = raster.load_config(config_path=config_path)

    yearly_ds, gapfill_ds = load_rasters(path='data/interim/raster',
                                         raster_version=raster_version,
                                         study_area=study_area,
                                         water_index=water_index,
                                         start_year=2000)

    # Create output vector folder using supplied vector version string;
    # if no vector version is provided, copy this from raster version
    if vector_version is None:
        vector_version = raster_version
    output_dir = f'data/interim/vector/{vector_version}/' \
                 f'{study_area}_{vector_version}'
    os.makedirs(output_dir, exist_ok=True)

    ####################
    # Load vector data #
    ####################

    # Get bounding box to load data for
    bbox = gpd.GeoSeries(box(*array_bounds(height=yearly_ds.sizes['y'],
                                           width=yearly_ds.sizes['x'],
                                           transform=yearly_ds.transform)),
                         crs=yearly_ds.crs)

    # Tide points
    tide_points_gdf = (gpd.read_file(
        config['Input files']['coastal_points_path'],
        bbox=bbox).to_crs(yearly_ds.crs))

    # Study area polygon
    gridcell_gdf = (gpd.read_file(config['Input files']['coastal_grid_path'],
                                  bbox=bbox).set_index('id').to_crs(
                                      str(yearly_ds.crs)))
    gridcell_gdf.index = gridcell_gdf.index.astype(int).astype(str)
    gridcell_gdf = gridcell_gdf.loc[[str(study_area)]]

    # Load climate indices
    climate_df = load_climate_data(index='soi',
                                   years=(yearly_ds.year.min().item(),
                                          yearly_ds.year.max().item()),
                                   annual=True,
                                   detrend=True)

    ##############################
    # Extract shoreline contours #
    ##############################

    # If a waterbody mask is provided, use this to remove non-coastal
    # waterbodies and estuaries from the dataset. If not, use empty mask
    if config['Input files']['waterbody_mask_path']:

        # Generate waterbody mask
        print('Generating waterbody mask')
        waterbody_mask = waterbody_masking(
            input_data=config['Input files']['waterbody_mask_path'],
            modification_data=config['Input files']
            ['waterbody_modifications_path'],
            bbox=bbox,
            yearly_ds=yearly_ds)

    else:
        waterbody_mask = np.full(yearly_ds.geobox.shape, False, dtype=bool)
        
    # Mask dataset to focus on coastal zone only
    masked_ds = contours_preprocess(yearly_ds,
                                    gapfill_ds,
                                    water_index,
                                    index_threshold,
                                    waterbody_mask,
                                    tide_points_gdf,
                                    output_path=output_dir,
                                    buffer_pixels=25)

    # Extract contours
    contours_gdf = subpixel_contours(da=masked_ds,
                                     z_values=index_threshold,
                                     min_vertices=10,
                                     dim='year').set_index('year')

    ######################
    # Compute statistics #
    ######################

    # Extract statistics modelling points along baseline contour
    points_gdf = points_on_line(contours_gdf, baseline_year, distance=30)

    # If a rocky mask is provided, use this to clip data
    if config['Input files']['coastal_classification_path']:

        # Import coastline classification
        print('Clipping to non-rocky shorelines')
        coastal_classification_gdf = (gpd.read_file(
            config['Input files']['coastal_classification_path'],
            bbox=bbox).to_crs(yearly_ds.crs))

        # Clip to remove rocky shoreline points
        points_gdf = rocky_shores_clip(points_gdf,
                                       coastal_classification_gdf,
                                       buffer=50)

    # If any points remain after rocky shoreline clip
    if points_gdf is not None:

        # Calculate annual coastline movements and residual tide heights
        # for every contour compared to the baseline year
        points_gdf = annual_movements(points_gdf, contours_gdf, yearly_ds,
                                      baseline_year, water_index)

        # Calculate regressions
        points_gdf = calculate_regressions(points_gdf,
                                           contours_gdf,
                                           climate_df)

        # Add count and span of valid obs, Shoreline Change Envelope
        # (SCE), Net Shoreline Movement (NSM) and Max/Min years
        stats_list = [
            'valid_obs', 'valid_span', 'sce', 'nsm', 'max_year', 'min_year'
        ]
        points_gdf[stats_list] = points_gdf.apply(
            lambda x: all_time_stats(x, initial_year=1988), axis=1)

        ################
        # Export stats #
        ################

        if points_gdf is not None:

            # Set up scheme to optimise file size
            schema_dict = {
                key: 'float:8.2'
                for key in points_gdf.columns
                if key != 'geometry'
            }
            schema_dict.update({
                'sig_time': 'float:8.3',
                'outl_time': 'str:80',
                'sig_soi': 'float:8.3',
                'outl_soi': 'str:80',
                'valid_obs': 'int:4',
                'valid_span': 'int:4',
                'max_year': 'int:4',
                'min_year': 'int:4',
            })
            col_schema = schema_dict.items()

            # Clip stats to study area extent
            stats_path = f'{output_dir}/ratesofchange_' \
                         f'{study_area}_{vector_version}_' \
                         f'{water_index}_{index_threshold:.2f}'
            points_gdf = points_gdf[points_gdf.intersects(
                gridcell_gdf.geometry.item())]

            # Export to GeoJSON
            points_gdf.to_crs('EPSG:4326').to_file(f'{stats_path}.geojson',
                                                   driver='GeoJSON')

            # Export as ESRI shapefiles
            points_gdf.to_file(f'{stats_path}.shp',
                               schema={
                                   'properties': col_schema,
                                   'geometry': 'Point'
                               })

    ###################
    # Export contours #
    ###################

    # Assign certainty to contours based on underlying masks
    contours_gdf = contour_certainty(contours_gdf, output_path=output_dir)

    # Add maturity details
    contours_gdf['maturity'] = 'final'
    contours_gdf.loc[contours_gdf.index == baseline_year,
                     'maturity'] = 'interim'

    # Clip annual shoreline contours to study area extent
    contour_path = f'{output_dir}/annualshorelines_' \
                   f'{study_area}_{vector_version}_' \
                   f'{water_index}_{index_threshold:.2f}'
    contours_gdf['geometry'] = contours_gdf.intersection(
        gridcell_gdf.geometry.item())
    contours_gdf.reset_index().to_crs('EPSG:4326').to_file(
        f'{contour_path}.geojson', driver='GeoJSON')

    # Export stats and contours as ESRI shapefiles
    contours_gdf.reset_index().to_file(f'{contour_path}.shp')


if __name__ == "__main__":
    generate_vectors()
