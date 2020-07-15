#!/usr/bin/env python
# coding: utf-8

import os
import sys
import glob
import numpy as np
import pandas as pd
import xarray as xr
import geopandas as gpd
from scipy import stats
from affine import Affine
from shapely.geometry import box
from shapely.geometry import LineString
from shapely.geometry import MultiLineString
from shapely.ops import nearest_points
from rasterio.features import rasterize
from rasterio.transform import array_bounds
from skimage.measure import label
from skimage.measure import find_contours
from skimage.morphology import binary_opening
from skimage.morphology import binary_erosion
from skimage.morphology import binary_dilation
from skimage.morphology import disk, square
from datacube.utils.cog import write_cog

import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

sys.path.append('/g/data/r78/rt1527/dea-notebooks/Scripts')
from dea_spatialtools import xr_vectorize


def subpixel_contours(da,
                      z_values=[0.0],
                      crs=None,
                      affine=None,
                      attribute_df=None,
                      output_path=None,
                      min_vertices=2,
                      dim='time',
                      errors='ignore'):
    
    """
    Uses `skimage.measure.find_contours` to extract multiple z-value 
    contour lines from a two-dimensional array (e.g. multiple elevations
    from a single DEM), or one z-value for each array along a specified 
    dimension of a multi-dimensional array (e.g. to map waterlines 
    across time by extracting a 0 NDWI contour from each individual 
    timestep in an xarray timeseries).    
    
    Contours are returned as a geopandas.GeoDataFrame with one row per 
    z-value or one row per array along a specified dimension. The 
    `attribute_df` parameter can be used to pass custom attributes 
    to the output contour features.
    
    Last modified: June 2020
    
    Parameters
    ----------  
    da : xarray DataArray
        A two-dimensional or multi-dimensional array from which 
        contours are extracted. If a two-dimensional array is provided, 
        the analysis will run in 'single array, multiple z-values' mode 
        which allows you to specify multiple `z_values` to be extracted.
        If a multi-dimensional array is provided, the analysis will run 
        in 'single z-value, multiple arrays' mode allowing you to 
        extract contours for each array along the dimension specified 
        by the `dim` parameter.  
    z_values : int, float or list of ints, floats
        An individual z-value or list of multiple z-values to extract 
        from the array. If operating in 'single z-value, multiple 
        arrays' mode specify only a single z-value.
    crs : string or CRS object, optional
        An EPSG string giving the coordinate system of the array 
        (e.g. 'EPSG:3577'). If none is provided, the function will 
        attempt to extract a CRS from the xarray object's `crs` 
        attribute.
    affine : affine.Affine object, optional
        An affine.Affine object (e.g. `from affine import Affine; 
        Affine(30.0, 0.0, 548040.0, 0.0, -30.0, "6886890.0) giving the 
        affine transformation used to convert raster coordinates 
        (e.g. [0, 0]) to geographic coordinates. If none is provided, 
        the function will attempt to obtain an affine transformation 
        from the xarray object (e.g. either at `da.transform` or
        `da.geobox.transform`).
    output_path : string, optional
        The path and filename for the output shapefile.
    attribute_df : pandas.Dataframe, optional
        A pandas.Dataframe containing attributes to pass to the output
        contour features. The dataframe must contain either the same 
        number of rows as supplied `z_values` (in 'multiple z-value, 
        single array' mode), or the same number of rows as the number 
        of arrays along the `dim` dimension ('single z-value, multiple 
        arrays mode').
    min_vertices : int, optional
        The minimum number of vertices required for a contour to be 
        extracted. The default (and minimum) value is 2, which is the 
        smallest number required to produce a contour line (i.e. a start
        and end point). Higher values remove smaller contours, 
        potentially removing noise from the output dataset.
    dim : string, optional
        The name of the dimension along which to extract contours when 
        operating in 'single z-value, multiple arrays' mode. The default
        is 'time', which extracts contours for each array along the time
        dimension.
    errors : string, optional
        If 'raise', then any failed contours will raise an exception.
        If 'ignore' (the default), a list of failed contours will be
        printed. If no contours are returned, an exception will always
        be raised.
        
    Returns
    -------
    output_gdf : geopandas geodataframe
        A geopandas geodataframe object with one feature per z-value 
        ('single array, multiple z-values' mode), or one row per array 
        along the dimension specified by the `dim` parameter ('single 
        z-value, multiple arrays' mode). If `attribute_df` was 
        provided, these values will be included in the shapefile's 
        attribute table.
    """

    def contours_to_multiline(da_i, z_value, min_vertices=2):
        '''
        Helper function to apply marching squares contour extraction
        to an array and return a data as a shapely MultiLineString.
        The `min_vertices` parameter allows you to drop small contours 
        with less than X vertices.
        '''
        
        # Extracts contours from array, and converts each discrete
        # contour into a Shapely LineString feature

        try:
            line_features = [LineString(i[:,[1, 0]]) 
                             for i in find_contours(da_i.data, z_value) 
                             if i.shape[0] > min_vertices]
            
        except:
            line_features = [LineString(i[:,[1, 0]]) 
                             for i in find_contours(da_i.data, z_value, 
                                                    fully_connected='high') 
                             if i.shape[0] > min_vertices]          

        # Output resulting lines into a single combined MultiLineString
        return MultiLineString(line_features)

    # Check if CRS is provided as a xarray.DataArray attribute.
    # If not, require supplied CRS
    try:
        crs = da.crs
    except:
        if crs is None:
            raise Exception("Please add a `crs` attribute to the "
                            "xarray.DataArray, or provide a CRS using the "
                            "function's `crs` parameter (e.g. 'EPSG:3577')")

    # Check if Affine transform is provided as a xarray.DataArray method.
    # If not, require supplied Affine
    try:
        affine = da.geobox.transform
    except KeyError:
        affine = da.transform
    except:
        if affine is None:
            raise Exception("Please provide an Affine object using the "
                            "`affine` parameter (e.g. `from affine import "
                            "Affine; Affine(30.0, 0.0, 548040.0, 0.0, -30.0, "
                            "6886890.0)`")

    # If z_values is supplied is not a list, convert to list:
    z_values = z_values if (isinstance(z_values, list) or 
                            isinstance(z_values, np.ndarray)) else [z_values]

    # Test number of dimensions in supplied data array
    if len(da.shape) == 2:

        print(f'Operating in multiple z-value, single array mode')
        dim = 'z_value'
        contour_arrays = {str(i)[0:10]: 
                          contours_to_multiline(da, i, min_vertices) 
                          for i in z_values}    

    else:

        # Test if only a single z-value is given when operating in 
        # single z-value, multiple arrays mode
        print(f'Operating in single z-value, multiple arrays mode')
        if len(z_values) > 1:
            raise Exception('Please provide a single z-value when operating '
                            'in single z-value, multiple arrays mode')

        contour_arrays = {str(i)[0:10]: 
                          contours_to_multiline(da_i, z_values[0], min_vertices) 
                          for i, da_i in da.groupby(dim)}

    # If attributes are provided, add the contour keys to that dataframe
    if attribute_df is not None:

        try:
            attribute_df.insert(0, dim, contour_arrays.keys())
        except ValueError:

            raise Exception("One of the following issues occured:\n\n"
                            "1) `attribute_df` contains a different number of "
                            "rows than the number of supplied `z_values` ("
                            "'multiple z-value, single array mode')\n"
                            "2) `attribute_df` contains a different number of "
                            "rows than the number of arrays along the `dim` "
                            "dimension ('single z-value, multiple arrays mode')")

    # Otherwise, use the contour keys as the only main attributes
    else:
        attribute_df = list(contour_arrays.keys())

    # Convert output contours to a geopandas.GeoDataFrame
    contours_gdf = gpd.GeoDataFrame(data=attribute_df, 
                                    geometry=list(contour_arrays.values()),
                                    crs=crs)   

    # Define affine and use to convert array coords to geographic coords.
    # We need to add 0.5 x pixel size to the x and y to obtain the centre 
    # point of our pixels, rather than the top-left corner
    shapely_affine = [affine.a, affine.b, affine.d, affine.e, 
                      affine.xoff + affine.a / 2.0, 
                      affine.yoff + affine.e / 2.0]
    contours_gdf['geometry'] = contours_gdf.affine_transform(shapely_affine)

    # Rename the data column to match the dimension
    contours_gdf = contours_gdf.rename({0: dim}, axis=1)

    # Drop empty timesteps
    empty_contours = contours_gdf.geometry.is_empty
    failed = ', '.join(map(str, contours_gdf[empty_contours][dim].to_list()))
    contours_gdf = contours_gdf[~empty_contours]

    # Raise exception if no data is returned, or if any contours fail
    # when `errors='raise'. Otherwise, print failed contours
    if empty_contours.all():
        raise Exception("Failed to generate any valid contours; verify that "
                        "values passed to `z_values` are valid and present "
                        "in `da`")
    elif empty_contours.any() and errors == 'raise':
        raise Exception(f'Failed to generate contours: {failed}')
    elif empty_contours.any() and errors == 'ignore':
        print(f'Failed to generate contours: {failed}')

    # If asked to write out file, test if geojson or shapefile
    if output_path and output_path.endswith('.geojson'):
        print(f'Writing contours to {output_path}')
        contours_gdf.to_crs({'init': 'EPSG:4326'}).to_file(filename=output_path, 
                                                           driver='GeoJSON')
    if output_path and output_path.endswith('.shp'):
        print(f'Writing contours to {output_path}')
        contours_gdf.to_file(filename=output_path)
        
    return contours_gdf


def outlier_mad(points, thresh=3.5):
    """
    Returns a boolean array with True if points are outliers and False 
    otherwise.

    Parameters:
    -----------
    points : 
        An numobservations by numdimensions array of observations
    thresh : 
        The modified z-score to use as a threshold. Observations with a 
        modified z-score (based on the median absolute deviation) greater
        than this value will be classified as outliers.

    Returns:
    --------
    mask : 
        A numobservations-length boolean array.

    References:
    ----------
    Source: https://github.com/joferkington/oost_paper_code/blob/master/utilities.py
    
    Boris Iglewicz and David Hoaglin (1993), "Volume 16: How to Detect and
    Handle Outliers", The ASQC Basic References in Quality Control:
    Statistical Techniques, Edward F. Mykytka, Ph.D., Editor. 
    """
    if len(points.shape) == 1:
        points = points[:,None]
    median = np.median(points, axis=0)
    diff = np.sum((points - median)**2, axis=-1)
    diff = np.sqrt(diff)
    med_abs_deviation = np.median(diff)

    modified_z_score = 0.6745 * diff / med_abs_deviation

    return modified_z_score > thresh


def change_regress(row, 
                   x_vals, 
                   x_labels, 
                   threshold=3.5, 
                   detrend_params=None,
                   slope_var='slope', 
                   interc_var='intercept',
                   pvalue_var='pvalue', 
                   stderr_var='stderr',
                   outliers_var='outliers'):
    
    # Extract x (time) and y (distance) values
    x = x_vals
    y = row.values.astype(np.float)
    
    # Drop NAN rows
    xy_df = np.vstack([x, y]).T
    is_valid = ~np.isnan(xy_df).any(axis=1)
    xy_df = xy_df[is_valid]
    valid_labels = x_labels[is_valid]
    
    # If detrending parameters are provided, apply these to the data to
    # remove the trend prior to running the regression
    if detrend_params:
        xy_df[:,1] = xy_df[:,1]-(detrend_params[0]*xy_df[:,0]+detrend_params[1])    
    
    # Remove outliers
    outlier_bool = ~outlier_mad(xy_df, thresh=threshold)
    xy_df = xy_df[outlier_bool]
        
    # Compute linear regression
    lin_reg = stats.linregress(x=xy_df[:,0], 
                               y=xy_df[:,1])  
       
    # Return slope, p-values and list of outlier years excluded from regression   
    return pd.Series({slope_var: np.round(lin_reg.slope, 3), 
                      interc_var: np.round(lin_reg.intercept, 3),
                      pvalue_var: np.round(lin_reg.pvalue, 3),
                      stderr_var: np.round(lin_reg.stderr, 3),
                      outliers_var: ' '.join(map(str, valid_labels[~outlier_bool]))})


def breakpoints(x, labels, model='rbf', pen=10, min_size=2, jump=1):
    '''
    Takes an array of erosion values, and returns a list of 
    breakpoint years
    '''
    signal = x.values
    algo = rpt.Pelt(model=model, min_size=min_size, jump=jump).fit(signal)
    result = algo.predict(pen=pen)
    if len(result) > 1:
        return [labels[i] for i in result[0:-1]][0]
    else:
        return None

    
def mask_ocean(bool_array, points_gdf, connectivity=1):
    '''
    Identifies ocean by selecting the largest connected area of water
    pixels, then dilating this region by 1 pixel to include mixed pixels
    '''
    
    # First, break boolean array into unique, discrete regions/blobs
    blobs_labels = xr.apply_ufunc(label, bool_array, None, 0, False, connectivity)
    
    # Get blob ID for each tidal modelling point
    x = xr.DataArray(points_gdf.geometry.x, dims='z')
    y = xr.DataArray(points_gdf.geometry.y, dims='z')   
    ocean_blobs = np.unique(blobs_labels.interp(x=x, y=y, method='nearest'))

    # Return only blobs that contained tide modelling point
    ocean_mask = blobs_labels.isin(ocean_blobs[ocean_blobs != 0])
    
    # Dilate mask so that we include land pixels on the inland side
    # of each shoreline to ensure contour extraction accurately
    # seperates land and water spectra
    ocean_mask = binary_dilation(ocean_mask, selem=square(3))

    return ocean_mask


def load_rasters(output_name, 
                 study_area, 
                 water_index='mndwi'):
    
    # Get file paths
    gapfill_index_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_{water_index}_gapfill.tif'))
    gapfill_tide_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_tide_m_gapfill.tif'))
    gapfill_count_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_count_gapfill.tif'))
    index_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_{water_index}.tif'))[1:len(gapfill_index_files)+1]
    stdev_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_stdev.tif'))[1:len(gapfill_index_files)+1]
    tidem_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_tide_m.tif'))[1:len(gapfill_index_files)+1]
    count_files = sorted(glob.glob(f'output_data/{study_area}_{output_name}/*_count.tif'))[1:len(gapfill_index_files)+1]

    # Test if data was returned
    if len(index_files) == 0:
        raise ValueError(f"No rasters found for grid cell {study_area} "
                         f"(analysis name '{output_name}'). Verify that "
                         f"`deacoastlines_generation.py` has been run "
                         "for this grid cell.")
    
    # Create variable used for time axis
    time_var = xr.Variable('year', [int(i.split('/')[2][0:4]) for i in index_files])

    # Import data
    index_da = xr.concat([xr.open_rasterio(i) for i in index_files], dim=time_var)
    gapfill_index_da = xr.concat([xr.open_rasterio(i) for i in gapfill_index_files], dim=time_var)
    gapfill_tide_da = xr.concat([xr.open_rasterio(i) for i in gapfill_tide_files], dim=time_var)
    gapfill_count_da = xr.concat([xr.open_rasterio(i) for i in gapfill_count_files], dim=time_var)
    stdev_da = xr.concat([xr.open_rasterio(i) for i in stdev_files], dim=time_var)
    tidem_da = xr.concat([xr.open_rasterio(i) for i in tidem_files], dim=time_var)
    count_da = xr.concat([xr.open_rasterio(i) for i in count_files], dim=time_var)

    # Assign names to allow merge
    index_da.name = water_index
    gapfill_index_da.name = 'gapfill_index'
    gapfill_tide_da.name = 'gapfill_tide_m'
    gapfill_count_da.name = 'gapfill_count'
    stdev_da.name = 'stdev'
    tidem_da.name = 'tide_m'
    count_da.name = 'count'

    # Combine into a single dataset and set CRS
    yearly_ds = xr.merge([index_da, gapfill_index_da, gapfill_tide_da, 
                          gapfill_count_da, stdev_da, tidem_da, count_da]).squeeze('band', drop=True)
    yearly_ds.attrs['crs'] = index_da.crs
    yearly_ds.attrs['transform'] = Affine(*index_da.transform)

    return yearly_ds


def waterbody_mask(input_data,
                   modification_data,
                   bbox,
                   yearly_ds):
    """
    Generates a raster mask for DEACoastLines based on the 
    SurfaceHydrologyPolygonsRegional.gdb dataset, and a vector 
    file containing minor modifications to this dataset (e.g. 
    features to remove or add to the dataset).
    
    The mask returns True for perennial 'Lake' features, any 
    'Aquaculture Area', 'Estuary', 'Watercourse Area', 'Salt 
    Evaporator', and 'Settling Pond' features. Features of 
    type 'add' from the modification data file are added to the
    mask, while features of type 'remove' are removed.
    """

    # Import SurfaceHydrologyPolygonsRegional data
    waterbody_gdf = gpd.read_file(input_data, bbox=bbox).to_crs(yearly_ds.crs)

    # Restrict to coastal features
    lakes_bool = ((waterbody_gdf.FEATURETYPE == 'Lake') &
                  (waterbody_gdf.PERENNIALITY == 'Perennial'))
    other_bool = waterbody_gdf.FEATURETYPE.isin(['Aquaculture Area', 
                                                 'Estuary', 
                                                 'Watercourse Area', 
                                                 'Salt Evaporator', 
                                                 'Settling Pond'])
    waterbody_gdf = waterbody_gdf[lakes_bool | other_bool]

    # Load in modification dataset and select features to remove/add
    mod_gdf = gpd.read_file(modification_data, bbox=bbox).to_crs(yearly_ds.crs)
    to_remove = mod_gdf[mod_gdf['type'] == 'remove']
    to_add = mod_gdf[mod_gdf['type'] == 'add']

    # Remove and add features
    if len(to_remove.index) > 0:
        if len(waterbody_gdf.index) > 0:
            waterbody_gdf = gpd.overlay(waterbody_gdf, to_remove, how='difference')        
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


def contours_preprocess(yearly_ds, 
                        water_index, 
                        index_threshold, 
                        waterbody_array, 
                        points_gdf,
                        output_path,
                        buffer_pixels=33):  
    
    # Flag nodata pixels
    nodata = yearly_ds[water_index].isnull()
    
    # Identify pixels with less than 5 annual observations or > 0.25 
    # MNDWI standard deviation in more than 25% of years. Apply binary 
    # erosion to isolate large connected areas of problematic pixels
    mean_stdev = (yearly_ds['stdev'] > 0.25).where(~nodata).mean(dim='year')
    mean_count = (yearly_ds['count'] < 5).where(~nodata).mean(dim='year')
    persistent_stdev = binary_erosion(mean_stdev > 0.5, selem = disk(2))
    persistent_lowobs = binary_erosion(mean_count > 0.5, selem = disk(2))

    # Remove low obs pixels and replace with 3-year gapfill
    # TODO: simplify by substituting entire identical gapfill array
    gapfill_mask = yearly_ds['count'] > 5
    yearly_ds[water_index] = (yearly_ds[water_index]
                              .where(gapfill_mask, 
                                     other=yearly_ds.gapfill_index))
    yearly_ds['tide_m'] = (yearly_ds['tide_m']
                           .where(gapfill_mask, 
                                  other=yearly_ds.gapfill_tide_m))

    # Apply water index threshold, restore nodata values back to NaN, 
    # and assign pixels within waterbody mask to 0 so they are excluded
    thresholded_ds = ((yearly_ds[water_index] > index_threshold)
                      .where(~nodata).where(~waterbody_array, 0))
    
    # Identify ocean by identifying the largest connected area of water pixels
    # as water in at least 90% of the entire stack of thresholded data.
    # Apply a binary opening step to clean noisy pixels
    all_time = thresholded_ds.mean(dim='year') > 0.9
    all_time_cleaned = xr.apply_ufunc(binary_opening, all_time, disk(3))
    all_time_ocean = mask_ocean(all_time_cleaned, points_gdf)   
    
    # Generate coastal buffer (30m * `buffer_pixels`) from ocean-land boundary
    buffer_ocean = binary_dilation(all_time_ocean, disk(buffer_pixels))
    buffer_land = binary_dilation(~all_time_ocean, disk(buffer_pixels))
    coastal_buffer = buffer_ocean & buffer_land    
    
    # Generate annual masks by selecting only water pixels that are 
    # directly connected to the ocean in each yearly timestep
    annual_masks = (thresholded_ds.groupby('year')
                    .apply(lambda x: mask_ocean(x, points_gdf)))
    
    # Keep pixels within both all time coastal buffer and annual mask
    masked_ds = yearly_ds[water_index].where(annual_masks & coastal_buffer)
    
    # Create raster containg all time mask data
    all_time_mask = np.full(yearly_ds.geobox.shape, 0, dtype='int8')
    all_time_mask[buffer_land & ~coastal_buffer] = 1
    all_time_mask[buffer_ocean & ~coastal_buffer] = 2
    all_time_mask[waterbody_array & coastal_buffer] = 3
    all_time_mask[persistent_stdev & coastal_buffer] = 4
    all_time_mask[persistent_lowobs & coastal_buffer] = 5

    # Export mask raster to assist evaluating results
    all_time_mask_da = xr.DataArray(data = all_time_mask, 
                                    coords={'x': yearly_ds.x, 
                                            'y': yearly_ds.y},
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


def stats_points(contours_gdf, baseline_year, distance=30):
    
    # Set annual shoreline to use as a baseline
    baseline_contour = contours_gdf.loc[[baseline_year]].geometry
    
    # If multiple features are returned, take unary union
    if baseline_contour.shape[0] > 0:
        baseline_contour = baseline_contour.unary_union
    else:
        baseline_contour = baseline_contour.iloc[0]

    # Generate points along line and convert to geopandas.GeoDataFrame
    points_line = [baseline_contour.interpolate(i) 
                   for i in range(0, int(baseline_contour.length), distance)]
    points_gdf = gpd.GeoDataFrame(geometry=points_line, crs=contours_gdf.crs)
    
    return points_gdf


def rocky_shores_clip(points_gdf, smartline_gdf, buffer=50):

    rocky = [
               'Bedrock breakdown debris (cobbles/boulders)',
               'Boulder (rock) beach',
               'Cliff (>5m) (undiff)',
               'Colluvium (talus) undiff',
               'Flat boulder deposit (rock) undiff',
               'Hard bedrock shore',
               'Hard bedrock shore inferred',
               'Hard rock cliff (>5m)',
               'Hard rocky shore platform',
               'Rocky shore (undiff)',
               'Rocky shore platform (undiff)',
               'Sloping hard rock shore',
               'Sloping rocky shore (undiff)',
               'Soft `bedrock¿ cliff (>5m)',
               'Steep boulder talus',
               'Hard rocky shore platform'
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
        rocky_shore_buffer = (gpd.overlay(rocky_gdf, 
                                          nonrocky_gdf, 
                                          how='difference')
                              .geometry
                              .unary_union)
        
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


def azimuth(point1, point2):
    '''
    Azimuth between two shapely points (interval 0 - 360)
    '''
    angle = np.arctan2(point2.x - point1.x, point2.y - point1.y)
    return np.degrees(angle) if angle > 0 else np.degrees(angle) + 360

    
def annual_movements(yearly_ds, 
                     points_gdf, 
                     tide_points_gdf, 
                     contours_gdf, 
                     baseline_year, 
                     water_index):

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
        points_gdf[f'p_{comp_year}'] = points_gdf.apply(lambda x: 
                                                        nearest_points(x.p_baseline, 
                                                                       comp_contour)[1], 
                                                        axis=1)

#         Compute distance between baseline and comparison year points and add
#         this distance as a new field named by the current year being analysed
        points_gdf[f'dist_{comp_year}'] = points_gdf.apply(
            lambda x: x.geometry.distance(x[f'p_{comp_year}']), axis=1)
        
#         # Angle test
#         points_gdf[f'{comp_year}'] = points_gdf.apply(lambda x: 
#                                                       azimuth(x.geometry, x[f'p_{comp_year}']), 
#                                                       axis=1)        
        

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

        # Compute change directionality (negative = erosion, positive = accretion)    
        points_gdf['loss_gain'] = np.where(points_gdf.index_baseline_p2 > 
                                           points_gdf.index_comp_p1, 1, -1)
        points_gdf[f'dist_{comp_year}'] = (points_gdf[f'dist_{comp_year}'] * 
                                           points_gdf.loss_gain)

        # Add tide data
        tide_array = yearly_ds['tide_m'].sel(year=int(comp_year))
        tide_points_gdf[f'dist_{comp_year}'] = tide_array.interp(x=baseline_x_vals, 
                                                            y=baseline_y_vals)
  
    # Keep required columns
    to_keep = points_gdf.columns.str.contains('dist|geometry')
    points_gdf = points_gdf.loc[:, to_keep]
    points_gdf = points_gdf.assign(**{f'dist_{baseline_year}': 0.0})
    points_gdf = points_gdf.round(2)    
    
    return points_gdf, tide_points_gdf


def calculate_regressions(contours_gdf, 
                          points_gdf, 
                          tide_points_gdf, 
                          climate_df):

    # Restrict climate and points data to years in datasets
    x_years = contours_gdf.index.unique().astype(int).values
    dist_years = [f'dist_{i}' for i in x_years]  
    points_subset = points_gdf[dist_years]

#     tide_subset = tide_points_gdf[x_years.astype(str)]
#     climate_subset = climate_df.loc[x_years, :]

    # Compute coastal change rates by linearly regressing annual movements vs. time
    print(f'Comparing annual movements with time')
    rate_out = (points_subset
                .apply(lambda x: change_regress(row=x,
                                                x_vals=x_years,
                                                x_labels=x_years), axis=1))
    points_gdf[['rate_time', 'incpt_time', 'sig_time', 'se_time', 'outl_time']] = rate_out

#     # Compute whether coastal change estimates are influenced by tide by linearly
#     # regressing residual tide heights against annual movements. A significant 
#     # relationship indicates that it may be difficult to isolate true erosion/
#     # accretion from the influence of tide
#     print(f'Comparing annual movements with tide heights')
#     tide_out = (tide_subset
#                 .apply(lambda x: change_regress(row=points_subset.iloc[x.name],
#                                                 x_vals=x, 
#                                                 x_labels=x_years), axis=1))
#     points_gdf[['rate_tide', 'incpt_tide', 'sig_tide', 'outl_tide']] = tide_out 

#     # Identify possible relationships between climate indices and coastal change 
#     # by linearly regressing climate indices against annual movements. Significant 
#     # results indicate that annual movements may be influenced by climate phenomena
#     for ci in climate_subset:

#         print(f'Comparing annual movements with {ci}')

#         # Compute stats for each row
#         ci_out = (points_subset
#                   .apply(lambda x: change_regress(row=x, 
#                                                   x_vals=climate_subset[ci].values, 
#                                                   x_labels=x_years), axis=1))

#         # Add data as columns  
#         points_gdf[[f'rate_{ci}', f'incpt_{ci}', f'sig_{ci}', f'outl_{ci}']] = ci_out

    # Set CRS
    points_gdf.crs = contours_gdf.crs

    # Custom sorting
    column_order = [
        'rate_time', 'sig_time', 'se_time', 'outl_time', 
        *dist_years, 'geometry'
    ]

    return points_gdf.loc[:, column_order]


def contour_certainty(contours_gdf, 
                      output_path, 
                      uncertain_classes=[4, 5]):

    # Load in mask data and identify uncertain classes
    all_time_mask = xr.open_rasterio(f'{output_path}/all_time_mask.tif')
    raster_mask = all_time_mask.isin(uncertain_classes) 
    
    # Vectorise mask data and fix any invalid geometries
    vector_mask = xr_vectorize(da=raster_mask,
                               crs=all_time_mask.geobox.crs,
                               transform=all_time_mask.geobox.transform,
                               mask=raster_mask.values)
    vector_mask.geometry = vector_mask.buffer(0).simplify(30)
    
    if len(vector_mask.index) > 0:

        # Clip and overlay to seperate into uncertain and certain classes
        contours_good = gpd.overlay(contours_gdf, vector_mask, how='difference')
        contours_good['certainty'] = 'good'
        contours_uncertain = gpd.clip(contours_gdf, vector_mask)
        contours_uncertain['certainty'] = 'uncertain'  

        # Combine both datasets and filter to line features
        contours_gdf = pd.concat([contours_good, contours_uncertain])
        is_line = contours_gdf.geometry.type.isin(['MultiLineString', 'LineString'])
        contours_gdf = contours_gdf.loc[is_line]    

        # Enforce index name (can be removed if one dataset is empty)
        contours_gdf.index.name = 'year'

    else:
        contours_gdf['certainty'] = 'good'   
        
    # Finally, set all 1991 and 1992 coastlines north of -23 degrees 
    # latitude to 'uncertain' due to Mt Pinatubo aerosol issue
    pinatubo_lat = ((contours_gdf.centroid.to_crs('EPSG:4326').y > -23) & 
                    (contours_gdf.index.isin(['1991', '1992'])))
    contours_gdf.loc[pinatubo_lat, 'certainty'] = 'uncertain'
    
    return contours_gdf


def all_time_stats(x, col='dist_'):

    # Select date columns only
    to_keep = x.index.str.contains(col)
    
    # Identify outlier years to drop from calculation
    to_drop = [f'{col}{i}' for i in x.outl_time.split(" ") if len(i) > 0]
    
    # Return matching subset of data
    subset = x.loc[to_keep].drop(to_drop).astype(float)

    # Calculate SCE range and max/min year
    return pd.Series({'sce': subset.max() - subset.min(),
                      'nsm': (subset.loc[subset.last_valid_index()] -
                              subset.loc[subset.first_valid_index()]),
                      'max_year': int(subset.idxmax()[-4:]),
                      'min_year': int(subset.idxmin()[-4:])})

    
def main(argv=None):
    
    #########
    # Setup #
    #########

    if argv is None:

        argv = sys.argv
        print(sys.argv)

    # If no user arguments provided
    if len(argv) < 3:

        str_usage = "You must specify a study area ID and name"
        print(str_usage)
        sys.exit()
        
    # Set study area and name for analysis
    study_area = int(argv[1])
    output_name = str(argv[2])
        
    # Set params
    water_index = 'mndwi'
    index_threshold = 0.00
    baseline_year = '2019'

    ###############################
    # Load DEA CoastLines rasters #
    ###############################
    
    yearly_ds = load_rasters(output_name, study_area, water_index)
    
    # Create output vector folder
    output_dir = f'output_data/{study_area}_{output_name}/vectors'
    os.makedirs(f'{output_dir}/shapefiles', exist_ok=True)

    ####################
    # Load vector data #
    ####################

    # Get bounding box to load data for
    bbox = gpd.GeoSeries(box(*array_bounds(height=yearly_ds.sizes['y'], 
                                           width=yearly_ds.sizes['x'], 
                                           transform=yearly_ds.transform)), 
                         crs=yearly_ds.crs)

    # Rocky shore mask
    smartline_gdf = (gpd.read_file('input_data/Smartline.gdb', 
                                   bbox=bbox)
                     .to_crs(yearly_ds.crs))

    # Tide points
    points_gdf = (gpd.read_file('input_data/tide_points_coastal.geojson', 
                                bbox=bbox)
                  .to_crs(yearly_ds.crs))

    # Study area polygon
    comp_gdf = (gpd.read_file('input_data/50km_albers_grid_clipped.geojson', 
                              bbox=bbox)
                .set_index('id')
                .to_crs(str(yearly_ds.crs)))

    # Mask to study area
    study_area_poly = comp_gdf.loc[study_area]

    # Load climate indices
    climate_df = pd.read_csv('input_data/climate_indices.csv', index_col='year')

    ##############################
    # Extract shoreline contours #
    ##############################

    # Generate waterbody mask
    waterbody_array = waterbody_mask(
        input_data='input_data/SurfaceHydrologyPolygonsRegional.gdb',
        modification_data='input_data/estuary_mask_modifications.geojson',
        bbox=bbox,
        yearly_ds=yearly_ds)

    # Mask dataset to focus on coastal zone only
    masked_ds = contours_preprocess(yearly_ds, 
        water_index, 
        index_threshold, 
        waterbody_array, 
        points_gdf,
        output_path=f'output_data/{study_area}_{output_name}')

    # Extract contours
    contours_gdf = subpixel_contours(da=masked_ds,
        z_values=index_threshold,
        min_vertices=30,
        dim='year').set_index('year')

    ######################
    # Compute statistics #
    ######################    
    
    # Extract statistics modelling points along baseline contour
    points_gdf = stats_points(contours_gdf, baseline_year, distance=30)
    
    # Clip to remove rocky shoreline points
    points_gdf = rocky_shores_clip(points_gdf, smartline_gdf, buffer=50)
    
    # If any points remain after rocky shoreline clip
    if points_gdf is not None:

        # Make a copy of the points GeoDataFrame to hold tidal data
        tide_points_gdf = points_gdf.copy()

        # Calculate annual coastline movements and residual tide heights 
        # for every contour compared to the baseline year
        points_gdf, tide_points_gdf = annual_movements(yearly_ds, 
                                                       points_gdf, 
                                                       tide_points_gdf, 
                                                       contours_gdf, 
                                                       baseline_year,
                                                       water_index)

        # Calculate regressions
        points_gdf = calculate_regressions(contours_gdf, 
                                           points_gdf, 
                                           tide_points_gdf, 
                                           climate_df)
        
        # Add in retreat/growth helper columns (used for web services)
        points_gdf['retreat'] = points_gdf.rate_time < 0 
        points_gdf['growth'] = points_gdf.rate_time > 0
        
        # Add Shoreline Change Envelope (SCE), Net Shoreline Movement 
        # (NSM) and Max/Min years
        points_gdf[['sce', 'nsm', 'max_year', 'min_year']] = points_gdf.apply(
            lambda x: deacl_stats.all_time_stats(x), axis=1)
        
        ################
        # Export stats #
        ################

        if points_gdf is not None:
            
            # Set up scheme to optimise file size
            schema_dict = {key: 'float:8.2' for key in points_gdf.columns
                           if key != 'geometry'}
            schema_dict.update({'sig_time': 'float:8.3',
                                'outl_time': 'str:80',
                                'retreat': 'bool', 
                                'growth': 'bool',
                                'max_year': 'int',
                                'min_year': 'int'})
            col_schema = schema_dict.items()
            
            # Clip stats to study area extent, remove rocky shores
            stats_path = f'{output_dir}/stats_{study_area}_{output_name}_' \
                         f'{water_index}_{index_threshold:.2f}'
            points_gdf = points_gdf[points_gdf.intersects(study_area_poly['geometry'])]

            # Export to GeoJSON
            points_gdf.to_crs('EPSG:4326').to_file(f'{stats_path}.geojson', 
                                                   driver='GeoJSON')

            # Export as ESRI shapefiles
            stats_path = stats_path.replace('vectors', 'vectors/shapefiles')
            points_gdf.to_file(f'{stats_path}.shp',
                               schema={'properties': col_schema,
                                       'geometry': 'Point'})
    
    ###################
    # Export contours #
    ###################    
    
    # Assign certainty to contours based on underlying masks
    contours_gdf = contour_certainty(
        contours_gdf=contours_gdf, 
        output_path=f'output_data/{study_area}_{output_name}')

    # Clip annual shoreline contours to study area extent
    contour_path = f'{output_dir}/contours_{study_area}_{output_name}_' \
                   f'{water_index}_{index_threshold:.2f}'
    contours_gdf['geometry'] = contours_gdf.intersection(study_area_poly['geometry'])
    contours_gdf.reset_index().to_crs('EPSG:4326').to_file(f'{contour_path}.geojson', 
                                                           driver='GeoJSON')

    # Export stats and contours as ESRI shapefiles
    contour_path = contour_path.replace('vectors', 'vectors/shapefiles')
    contours_gdf.reset_index().to_file(f'{contour_path}.shp')


if __name__ == "__main__":
    main()