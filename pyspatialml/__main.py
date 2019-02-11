import os
import tempfile
import re
from collections import namedtuple, OrderedDict
from itertools import chain
from functools import partial
from copy import deepcopy

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio import features
from rasterio.transform import Affine
from rasterio.windows import Window
from shapely.geometry import Point
from tqdm import tqdm

def from_files(file_path, mode='r'):
    """
    Create a Raster object from a GDAL-supported raster file, or list of files

    Args
    ----
    file_path : str, list
        File path, or list of file paths to GDAL-supported rasters

    mode : str
        File open mode. Mode must be one of 'r', 'r+', or 'w'

    Returns
    -------
    raster : pyspatialml.Raster object
    """

    if isinstance(file_path, str):
        file_path = [file_path]

    if mode not in ['r', 'r+', 'w']:
        raise ValueError("mode must be one of 'r', 'r+', or 'w'")

    # get band objects from datasets
    bands = []

    for f in file_path:
        src = rasterio.open(f, mode=mode)
        for i in range(src.count):
            band = rasterio.band(src, i+1)
            bands.append(RasterLayer(band))

    raster = Raster(bands)
    return raster


class BaseRaster(object):
    """
    Raster base class that contains methods that apply both to RasterLayer and
    Raster objects. Wraps a rasterio.band object, which is a named tuple
    consisting of the file path, the band index, the dtype and shape a
    individual band within a raster dataset
    """

    def __init__(self, band):
        self.shape = band.shape
        self.crs = band.ds.crs
        self.transform = band.ds.transform
        self.width = band.ds.width
        self.height = band.ds.height
        self.bounds = band.ds.bounds  # BoundingBox class (namedtuple) ('left', 'bottom', 'right', 'top')
        self.read = partial(band.ds.read, indexes=band.bidx)

        try:
            self.write = partial(band.ds.write, indexes=band.bidx)
        except AttributeError:
            pass

    def reproject(self):
        raise NotImplementedError

    def mask(self):
        raise NotImplementedError

    def resample(self):
        raise NotImplementedError

    def aggregate(self):
        raise NotImplementedError

    def calc(self, function, file_path=None, driver='GTiff', dtype='float32',
             nodata=-99999, progress=True):
        """
        Apply user-supplied function to a Raster object

        Args
        ----
        function : function that takes an numpy array as a single argument

        file_path : str, optional
            Path to a GeoTiff raster for the classification results
            If not supplied then output is written to a temporary file

        driver : str, optional. Default is 'GTiff'
            Named of GDAL-supported driver for file export

        dtype : str, optional. Default is 'float32'
            Numpy data type for file export

        nodata : any number, optional. Default is -99999
            Nodata value for file export

        progress : bool, optional. Default is True
            Show tqdm progress bar for prediction

        Returns
        -------
        pyspatialml.Raster object
        """

        # determine output dimensions
        window = Window(0, 0, 1, self.width)
        img = self.read(masked=True, window=window)
        arr = function(img)

        if len(arr.shape) > 2:
            indexes = range(arr.shape[0])
        else:
            indexes = 1

        count = len(indexes)

        # optionally output to a temporary file
        if file_path is None:
            file_path = tempfile.NamedTemporaryFile().name

        # open output file with updated metadata
        meta = self.meta
        meta.update(driver=driver, count=count, dtype=dtype, nodata=nodata)

        with rasterio.open(file_path, 'w', **meta) as dst:

            # define windows
            windows = [window for ij, window in dst.block_windows()]

            # generator gets raster arrays for each window
            data_gen = (self.read(window=window, masked=True) for window in windows)

            if progress is True:
                for window, arr, pbar in zip(windows, data_gen, tqdm(windows)):
                    result = function(arr)
                    result = np.ma.filled(result, fill_value=nodata)
                    dst.write(result.astype(dtype), window=window)
            else:
                for window, arr in zip(windows, data_gen):
                    result = function(arr)
                    result = np.ma.filled(result, fill_value=nodata)
                    dst.write(result.astype(dtype), window=window)

        return self._newraster(file_path)

    def crop(self, bounds, file_path=None, driver='GTiff', nodata=-99999):
        """
        Crops a Raster object by the supplied bounds

        Args
        ----
        bounds : tuple
            A tuple containing the bounding box to clip by in the
            form of (xmin, xmax, ymin, ymax)

        file_path : str, optional. Default=None
            File path to save to cropped raster.
            If not supplied then the cropped raster is saved to a
            temporary file

        driver : str, optional. Default is 'GTiff'
            Named of GDAL-supported driver for file export

        nodata : int, float
            Nodata value for cropped dataset

        Returns
        -------
        pyspatialml.Raster object cropped to new extent
        """

        xmin, ymin, xmax, ymax = bounds

        rows, cols = rasterio.transform.rowcol(
            self.transform, xs=(xmin, xmax), ys=(ymin, ymax))

        window = Window(col_off=min(cols),
                        row_off=min(rows),
                        width=max(cols)-min(cols),
                        height=max(rows)-min(rows))

        cropped_arr = self.read(masked=True, window=window)
        meta = self.meta
        aff = self.transform
        meta['width'] = max(cols) - min(cols)
        meta['height'] = max(rows) - min(rows)
        meta['transform'] = Affine(aff.a, aff.b, xmin, aff.d, aff.e, ymin)
        meta['driver'] = driver
        meta['nodata'] = nodata

        if file_path is None:
            file_path = tempfile.NamedTemporaryFile().name

        with rasterio.open(file_path, 'w', **meta) as dst:
            dst.write(cropped_arr)

        return self._newraster(file_path, self.names)

    def _newraster(self, file_path, names=None):
        """
        Return a new Raster object

        Args
        ----
        file_path : str
            Path to files to create the new Raster object from
        names : list, optional
            List to name the RasterLayer objects in the stack. If not supplied
            then the names will be generated from the filename

        Returns
        -------
        raster : pyspatialml.Raster object
        """

        raster = from_files(file_path)

        if names is not None:
            rename = {old : new for old, new in zip(raster.names, self.names)}
            raster.rename(rename)

        return raster

    def plot(self):
        raise NotImplementedError

    def sample(self, size, strata=None, return_array=False, random_state=None):
        """
        Generates a random sample of according to size, and samples the pixel
        values from a GDAL-supported raster

        Args
        ----
        size : int
            Number of random samples or number of samples per strata
            if strategy='stratified'

        strata : rasterio.io.DatasetReader, optional (default=None)
            To use stratified instead of random sampling, strata can be
            supplied using an open rasterio DatasetReader object

        return_array : bool, default = False
            Optionally return extracted data as separate X, y and xy
            masked numpy arrays

        na_rm : bool, default = True
            Optionally remove rows that contain nodata values

        random_state : int
            integer to use within random.seed

        Returns
        -------
        samples: array-like
            Numpy array of extracted raster values, typically 2d

        valid_coordinates: 2d array-like
            2D numpy array of xy coordinates of extracted values
        """

        # set the seed
        np.random.seed(seed=random_state)

        if not strata:

            # create np array to store randomly sampled data
            # we are starting with zero initial rows because data will be appended,
            # and number of columns are equal to n_features
            valid_samples = np.zeros((0, self.count))
            valid_coordinates = np.zeros((0, 2))

            # loop until target number of samples is satified
            satisfied = False

            n = size
            while satisfied is False:

                # generate random row and column indices
                Xsample = np.random.choice(range(0, self.width), n)
                Ysample = np.random.choice(range(0, self.height), n)

                # create 2d numpy array with sample locations set to 1
                sample_raster = np.empty((self.height, self.width))
                sample_raster[:] = np.nan
                sample_raster[Ysample, Xsample] = 1

                # get indices of sample locations
                rows, cols = np.nonzero(np.isnan(sample_raster) == False)

                # convert row, col indices to coordinates
                xy = np.transpose(rasterio.transform.xy(self.transform, rows, cols))

                # sample at random point locations
                samples = self.extract_xy(xy)

                # append only non-masked data to each row of X_random
                samples = samples.astype('float32').filled(np.nan)
                invalid_ind = np.isnan(samples).any(axis=1)
                samples = samples[~invalid_ind, :]
                valid_samples = np.append(valid_samples, samples, axis=0)

                xy = xy[~invalid_ind, :]
                valid_coordinates = np.append(
                    valid_coordinates, xy, axis=0)

                # check to see if target_nsamples has been reached
                if len(valid_samples) >= size:
                    satisfied = True
                else:
                    n = size - len(valid_samples)

        else:
            # get number of unique categories
            strata_arr = strata.read(1)
            categories = np.unique(strata_arr)
            categories = categories[np.nonzero(categories != strata.nodata)]
            categories = categories[~np.isnan(categories)]

            # store selected coordinates
            selected = np.zeros((0, 2))

            for cat in categories:

                # get row,col positions for cat strata
                ind = np.transpose(np.nonzero(strata_arr == cat))

                if size > ind.shape[0]:
                    msg = 'Sample size is greater than number of pixels in strata {0}'.format(str(ind))
                    msg = os.linesep.join([msg, 'Sampling using replacement'])
                    Warning(msg)

                # random sample
                sample = np.random.uniform(
                    low=0, high=ind.shape[0], size=size).astype('int')
                xy = ind[sample, :]

                selected = np.append(selected, xy, axis=0)

            # convert row, col indices to coordinates
            x, y = rasterio.transform.xy(
                self.transform, selected[:, 0], selected[:, 1])
            valid_coordinates = np.column_stack((x, y))

            # extract data
            valid_samples = self.extract_xy(valid_coordinates)

        # return as geopandas array as default (or numpy arrays)
        if return_array is False:
            gdf = pd.DataFrame(valid_samples, columns=self.names)
            gdf['geometry'] = list(zip(valid_coordinates[:, 0], valid_coordinates[:, 1]))
            gdf['geometry'] = gdf['geometry'].apply(Point)
            gdf = gpd.GeoDataFrame(gdf, geometry='geometry', crs=self.crs)
            return gdf
        else:
            return valid_samples, valid_coordinates

    def head(self):
        """
        Show the head (first rows, first columns) or tail
        (last rows, last columns) of the cells of a Raster object
        """

        window = Window(col_off=0, row_off=0, width=20, height=10)
        arr = self.read(window=window)

        return arr

    def tail(self):
        """
        Show the head (first rows, first columns) or tail
        (last rows, last columns) of the cells of a Raster object
        """

        window = Window(col_off=self.width-20,
                        row_off=self.height-10,
                        width=20,
                        height=10)
        arr = self.read(window=window)

        return arr

    def to_pandas(self, max_pixels=50000, resampling='nearest'):
        """
        Raster to pandas DataFrame

        Args
        ----
        max_pixels: int, default=50000
            Maximum number of pixels to sample

        resampling : str, default = 'nearest'
            Resampling method to use when applying decimated reads when
            out_shape is specified. Supported methods are: 'average',
            'bilinear', 'cubic', 'cubic_spline', 'gauss', 'lanczos',
            'max', 'med', 'min', 'mode', 'q1', 'q3'

        Returns
        -------
        df : pandas DataFrame
        """

        n_pixels = self.shape[0] * self.shape[1]
        scaling = max_pixels / n_pixels

        # read dataset using decimated reads
        out_shape = (round(self.shape[0] * scaling), round(self.shape[1] * scaling))
        arr = self.read(masked=True, out_shape=out_shape, resampling=resampling)

        if isinstance(self, RasterLayer):
            arr = arr[np.newaxis, :, :]

        # x and y grid coordinate arrays
        x_range = np.linspace(start=self.bounds.left, stop=self.bounds.right, num=arr.shape[2])
        y_range = np.linspace(start=self.bounds.top, stop=self.bounds.bottom, num=arr.shape[1])
        xs, ys = np.meshgrid(x_range, y_range)

        arr = arr.reshape((arr.shape[0], arr.shape[1] * arr.shape[2]))
        arr = arr.transpose()
        df = pd.DataFrame(np.column_stack((xs.flatten(), ys.flatten(), arr)),
                          columns=['x', 'y'] + self.names)

        # set nodata values to nan
        for i, col_name in enumerate(self.names):
            df.loc[df[col_name] == self.nodatavals[i], col_name] = np.nan

        return df


class RasterLayer(BaseRaster):
    """
    A single-band raster object that wraps selected attributes and methods from
    a rasterio.band object into a simpler class. Inherits attributes and
    methods from RasterBase. Contains methods that are only relevant to a
    single-band raster. A RasterLayer is initiated from an underlying
    rasterio.band object
    """

    def __init__(self, band):

        # access inherited methods/attributes overriden by __init__
        super().__init__(band)

        # rasterlayer specific attributes
        self.bidx = band.bidx
        self.dtype = band.dtype
        self.nodata = band.ds.nodata
        self.file = band.ds.files[0]
        self.driver = band.ds.meta['driver']
        self.ds = band.ds

    def fill(self):
        raise NotImplementedError

    def sieve(self):
        raise NotImplementedError

    def clump(self):
        raise NotImplementedError

    def focal(self):
        raise NotImplementedError


class Raster(BaseRaster):
    """
    Flexible class that represents a collection of file-based GDAL-supported
    raster datasets which share a common coordinate reference system and
    geometry. Raster objects encapsulate RasterLayer objects, which represent
    single band rasters that can physically be represented by separate
    single-band raster files, multi-band raster files, or any combination of
    individual bands from multi-band rasters and single-band rasters.
    RasterLayer objects only exist within Raster objects.

    A Raster object should be created using the pyspatialml.from_files()
    function, where a single file, or a list of files is passed as the file_path
    argument.

    Additional RasterLayer objects can be added to an existing Raster object
    using the append() method. Either the path to file(s) or an existing
    RasterLayer from another Raster object can be passed to this method and
    those layers, if they are spatially aligned, will be appended to the Raster
    object. Any RasterLayer can also be removed from a Raster object using the
    drop() method.
    """

    def __init__(self, layers):

        self.loc = OrderedDict()  # name-based indexing
        self.iloc = []            # index-based indexing
        self.names = []           # syntactically-valid names of datasets with appended band number
        self.files = []           # files that are linked to as RasterLayer objects
        self.dtypes = []          # dtypes of stacked raster datasets and bands
        self.nodatavals = []      # no data values of stacked raster datasets and bands
        self.count = 0            # number of bands in stacked raster datasets
        self.res = None           # (x, y) resolution of aligned raster datasets
        self.meta = None          # dict containing 'crs', 'transform', 'width', 'height', 'count', 'dtype'
        self._layers = None       # set proxy for self._files
        self.layers = layers      # call property

    def __getitem__(self, layername):
        """
        Get a RasterLayer within the Raster object using label-based indexing
        """

        if layername in self.names is False:
            raise AttributeError('layername not present in Raster object')

        return getattr(self, layername)

    def iterlayers(self):
        """
        Iterate over Raster object layers
        """

        for k, v in self.loc.items():
            yield k, v

    @property
    def layers(self):
        return self._layers

    @layers.setter
    def layers(self, layers):
        """
        Setter method for the files attribute in the Raster object
        """

        # some checks
        if isinstance(layers, RasterLayer):
            layers = [layers]

        if all(isinstance(x, type(layers[0])) for x in layers) is False:
            raise ValueError('Cannot create a Raster object from a mixture of input types')

        meta = self._check_alignment(layers)
        if meta is False:
            raise ValueError(
                'Raster datasets do not all have the same dimensions or transform')

        # reset existing attributes
        for name in self.names:
            delattr(self, name)
        self.iloc = []
        self.loc = OrderedDict()
        self.names = []
        self.files = []
        self.dtypes = []
        self.nodatavals = []

        # update global Raster object attributes with new values
        self.count = len(layers)
        self.width = meta['width']
        self.height = meta['height']
        self.shape = (self.height, self.width)
        self.transform = meta['transform']
        self.res = (abs(meta['transform'].a), abs(meta['transform'].e))
        self.crs = meta['crs']
        bounds = rasterio.transform.array_bounds(self.height, self.width, self.transform)
        BoundingBox = namedtuple('BoundingBox', ['left', 'bottom', 'right', 'top'])
        self.bounds = BoundingBox(bounds[0], bounds[1], bounds[2], bounds[3])
        self._layers = layers

        # update attributes per dataset
        for i, layer in enumerate(layers):
            valid_name = self._make_name(layer.file)
            self.dtypes.append(layer.dtype)
            self.nodatavals.append(layer.nodata)
            self.files.append(layer.file)

            if layer.ds.count > 1:
                valid_name = '_'.join([valid_name, str(layer.bidx)])

            self.names.append(valid_name)
            self.loc.update({valid_name: layer})
            self.iloc.append(layer)
            setattr(self, valid_name, layer)

        self.meta = dict(crs=self.crs,
                         transform=self.transform,
                         width=self.width,
                         height=self.height,
                         count=self.count,
                         dtype=self._maximum_dtype())

    @staticmethod
    def _check_alignment(layers):
        """
        Check that a list of rasters are aligned with the same pixel dimensions
        and geotransforms
        """

        src_meta = []
        for layer in layers:
            src_meta.append(layer.ds.meta.copy())

        if not all(i['crs'] == src_meta[0]['crs'] for i in src_meta):
            Warning('crs of all rasters does not match, '
                    'possible unintended consequences')

        if not all([i['height'] == src_meta[0]['height'] or
                    i['width'] == src_meta[0]['width'] or
                    i['transform'] == src_meta[0]['transform'] for i in src_meta]):
            return False
        else:
            return src_meta[0]

    def _make_name(self, name):
        """
        Converts a filename to a valid class attribute name

        Args
        ----
        name : str
            File name for convert to a valid class attribute name

        Returns
        -------
        valid_name : str
            Syntatically-correct name of layer so that it can form a class
            instance attribute
        """

        # replace spaces with underscore
        valid_name = os.path.basename(name)
        valid_name = valid_name.split(os.path.extsep)[0]
        valid_name = valid_name.replace(' ', '_')
        
        # ensure that does not start with number
        if valid_name[0].isdigit():
            valid_name = "x" + valid_name
        
        # remove parentheses and brackets
        valid_name = re.sub(r'[\[\]\(\)\{\}\;]','', valid_name)

        # check to see if same name already exists
        if valid_name in self.names:
            valid_name = '_'.join([valid_name, '1'])

        return valid_name

    def _maximum_dtype(self):
        """
        Returns a single dtype that is large enough to store data
        within all raster bands
        """

        if 'complex128' in self.dtypes:
            dtype = 'complex128'
        elif 'complex64' in self.dtypes:
            dtype = 'complex64'
        elif 'complex' in self.dtypes:
            dtype = 'complex'
        elif 'float64' in self.dtypes:
            dtype = 'float64'
        elif 'float32' in self.dtypes:
            dtype = 'float32'
        elif 'int32' in self.dtypes:
            dtype = 'int32'
        elif 'uint32' in self.dtypes:
            dtype = 'uint32'
        elif 'int16' in self.dtypes:
            dtype = 'int16'
        elif 'uint16' in self.dtypes:
            dtype = 'uint16'
        elif 'uint16' in self.dtypes:
            dtype = 'uint16'
        elif 'bool' in self.dtypes:
            dtype = 'bool'

        return dtype

    def read(self, masked=False, window=None, out_shape=None, resampling='nearest', **kwargs):
        """
        Reads data from the Raster object into a numpy array

        Overrides read BaseRaster class read method and replaces it with a
        method that reads from multiple RasterLayer objects

        Args
        ----
        masked : bool, optional, default = False
            Read data into a masked array

        window : rasterio.window.Window object, optional
            Tuple of col_off, row_off, width, height of a window of data
            to read

        out_shape : tuple, optional
            Shape of shape of array (rows, cols) to read data into using
            decimated reads

        resampling : str, default = 'nearest'
            Resampling method to use when applying decimated reads when
            out_shape is specified. Supported methods are: 'average',
            'bilinear', 'cubic', 'cubic_spline', 'gauss', 'lanczos',
            'max', 'med', 'min', 'mode', 'q1', 'q3'

        **kwargs : dict
            Other arguments to pass to rasterio.DatasetReader.read method

        Returns
        -------
        arr : ndarraySubnautica Below Zero is going into Early Access on Mac & Windows PC. We want to bring Below Zero to Xbox One and PlayStation 4 as soon as possible.
            Raster values in 3d numpy array [band, row, col]
        """

        dtype = self.meta['dtype']

        resampling_methods = [i.name for i in rasterio.enums.Resampling]
        if resampling not in resampling_methods:
            raise ValueError(
                'Invalid resampling method.' +
                'Resampling method must be one of {0}:'.format(
                    resampling_methods))

        # get window to read from window or height/width of dataset
        if window is None:
            width = self.width
            height = self.height
        else:
            width = window.width
            height = window.height

        # decimated reads using nearest neighbor resampling
        if out_shape:
            height, width = out_shape

        # read masked or non-masked data
        if masked is True:
            arr = np.ma.zeros((self.count, height, width), dtype=dtype)
        else:
            arr = np.zeros((self.count, height, width), dtype=dtype)

        # read bands separately into numpy array
        for i, layer in enumerate(self.iloc):
            arr[i, :, :] = layer.read(
                masked=masked,
                window=window,
                out_shape=out_shape,
                resampling=rasterio.enums.Resampling[resampling],
                **kwargs)

        return arr

    def write(self, file_path, driver="GTiff", dtype=None, nodata=None):
        """
        Write the Raster object to a file

        Overrides the write RasterBase class method, which is a partial
        function of the rasterio.DatasetReader.write method

        Args
        ----
        file_path : str
            File path to save the Raster object as a multiband file-based
            raster dataset

        driver : str, default = GTiff
            GDAL compatible driver

        dtype : str, optional
            Optionally specify a data type when saving to file. Otherwise
            a datatype is selected based on the RasterLayers in the stack

        nodata : int, float, optional
            Optionally assign a new nodata value when saving to file. Otherwise
            a nodata value that is appropriate for the dtype is used
        """

        if dtype is None:
            dtype = self.meta['dtype']

        if nodata is None:
            nodata = np.iinfo(dtype).min

        with rasterio.open(file_path, mode='w', driver=driver, nodata=nodata,
                           **self.meta) as dst:

            for i, layer in enumerate(self.iloc):
                arr = layer.read()
                arr[arr == layer.nodata] = nodata

                dst.write(arr.astype(dtype), i+1)

        return self._newraster(file_path)

    def predict(self, estimator, file_path=None, predict_type='raw',
                indexes=None, driver='GTiff', dtype='float32', nodata=-99999,
                progress=True):
        """
        Apply prediction of a scikit learn model to a pyspatialml.Raster object

        Args
        ----
        estimator : estimator object implementing 'fit'
            The object to use to fit the data

        file_path : str, optional
            Path to a GeoTiff raster for the classification results
            If not supplied then output is written to a temporary file

        predict_type : str, optional (default='raw')
            'raw' for classification/regression
            'prob' for probabilities

        indexes : List, int, optional
            List of class indices to export

        driver : str, optional. Default is 'GTiff'
            Named of GDAL-supported driver for file export

        dtype : str, optional. Default is 'float32'
            Numpy data type for file export

        nodata : any number, optional. Default is -99999
            Nodata value for file export

        progress : bool, optional. Default is True
            Show tqdm progress bar for prediction

        Returns
        -------
        Raster object
        """

        # chose prediction function
        if predict_type == 'raw':
            predfun = _predfun
        elif predict_type == 'prob':
            predfun = _probfun

        # determine output count
        if predict_type == 'prob' and isinstance(indexes, int):
            indexes = range(indexes, indexes + 1)

        elif predict_type == 'prob' and indexes is None:
            window = Window(0, 0, self.width, 1)
            img = self.read(masked=True, window=window)
            n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]
            n_samples = rows * cols
            flat_pixels = img.transpose(1, 2, 0).reshape(
                (n_samples, n_features))
            result = estimator.predict_proba(flat_pixels)
            indexes = np.arange(0, result.shape[1])

        elif predict_type == 'raw':
            indexes = range(1)

        # open output file with updated metadata
        meta = self.meta
        meta.update(driver=driver, count=len(indexes), dtype=dtype, nodata=nodata)

        # optionally output to a temporary file
        if file_path is None:
            file_path = tempfile.NamedTemporaryFile().name

        with rasterio.open(file_path, 'w', **meta) as dst:

            # define windows
            windows = [window for ij, window in dst.block_windows()]

            # generator gets raster arrays for each window
            data_gen = (self.read(window=window, masked=True) for window in windows)

            if progress is True:
                for window, arr, pbar in zip(windows, data_gen, tqdm(windows)):
                    result = predfun(arr, estimator)
                    result = np.ma.filled(result, fill_value=nodata)
                    dst.write(result[indexes, :, :].astype(dtype), window=window)
            else:
                for window, arr  in zip(windows, data_gen):
                    result = predfun(arr, estimator)
                    result = np.ma.filled(result, fill_value=nodata)
                    dst.write(result[indexes, :, :].astype(dtype), window=window)

        raster = from_files(file_path)
        if len(indexes) > 1:
            raster.names = ['_'.join(['prob', str(i+1)]) for i in range(raster.count)]

        return raster

    def append(self, other):
        """
        Setter method to add new Raster objects

        Args
        ----
        other : Raster object or list of Raster objects
        """
        
        if isinstance(other, Raster):
            other = [other]

        for new_raster in other:
            existing_names = deepcopy(self.names)
            other_names = deepcopy(new_raster.names)
        
            # update layers
            self.layers += new_raster.layers
            reset_names = self.names
            
            # generate dict to replace newly generated names with names from
            # the two existing Raster objects
            renamed = {reset_names[i]: newname for i, newname in enumerate(
                existing_names + other_names)}
            self.rename(renamed)

    def drop(self, labels):
        """
        Drop individual RasterLayers from a Raster object

        Args
        ----
        labels : single label or list-like
            Index (int) or layer name to drop. Can be a single integer or label,
            or a list of integers or labels
        """

        # convert single label to list
        if isinstance(labels, (str, int)):
            labels = [labels]

        if len([i for i in labels if isinstance(i, int)]) == len(labels):
            # numerical index based subsetting
            self.layers = [v for (i, v) in enumerate(self.layers) if i not in labels]
            self.names = [v for (i, v) in enumerate(self.names) if i not in labels]

        elif len([i for i in labels if isinstance(i, str)]) == len(labels):
            # str label based subsetting
            self.layers = [v for (i, v) in enumerate(self.layers) if self.names[i] not in labels]
            self.names = [v for (i, v) in enumerate(self.names) if self.names[i] not in labels]

        else:
            raise ValueError('Cannot drop layers based on mixture of indexes and labels')

    def rename(self, names):
        """
        Setter method to add new Raster objects

        Args
        ----
        other : Raster object or list of Raster objects
        """
        
        for old_name, new_name in names.items():
            # get layer and index of layer
            layer = self.loc[old_name]
            idx = self.names.index(old_name)
            
            # change name to new name
            self.names[idx] = new_name
            self.loc[new_name] = self.loc.pop(old_name)
            
            setattr(self, new_name, layer)
            
            # delete the attribute if it has changed
            if new_name != old_name:
                delattr(self, old_name)

    def extract_xy(self, xy):
        """
        Samples pixel values of a Raster using an array of xy locations

        Args
        ----
        xy : 2d array-like
            x and y coordinates from which to sample the raster (n_samples, xy)

        Returns
        -------
        values : 2d array-like
            Masked array containing sampled raster values (sample, bands)
            at x,y locations
        """

        # clip coordinates to extent of raster
        extent = self.bounds
        valid_idx = np.where((xy[:, 0] > extent.left) &
                             (xy[:, 0] < extent.right) &
                             (xy[:, 1] > extent.bottom) &
                             (xy[:, 1] < extent.top))[0]
        xy = xy[valid_idx, :]

        dtype = self._maximum_dtype()
        values = np.ma.zeros((xy.shape[0], self.count), dtype=dtype)
        rows, cols = rasterio.transform.rowcol(
            transform=self.transform, xs=xy[:, 0], ys=xy[:, 1])

        for i, (row, col) in enumerate(zip(rows, cols)):
            window = Window(col_off=col,
                            row_off=row,
                            width=1,
                            height=1)
            values[i, :] = self.read(masked=True, window=window).reshape((1, self.count))

        return values

    def _extract_by_indices(self, rows, cols):
        """
        Spatial query of Raster object (by-band)
        """

        X = np.ma.zeros((len(rows), self.count))

        for i, layer in enumerate(self.iloc):
            arr = layer.read(masked=True)
            X[:, i] = arr[rows, cols]

        return X

    def _clip_xy(self, xy, y=None):
        """
        Clip array of xy coordinates to extent of Raster object
        """

        extent = self.bounds
        valid_idx = np.where((xy[:, 0] > extent.left) &
                             (xy[:, 0] < extent.right) &
                             (xy[:, 1] > extent.bottom) &
                             (xy[:, 1] < extent.top))[0]
        xy = xy[valid_idx, :]

        if y is not None:
            y = y[valid_idx]

        return xy, y

    def extract_vector(self, response, field=None, return_array=False,
                       duplicates='keep', na_rm=True, low_memory=False):
        """
        Sample a Raster object by a geopandas GeoDataframe containing points,
        lines or polygon features

        Args
        ----
        response: Geopandas DataFrame
            Containing either point, line or polygon geometries. Overlapping
            geometries will cause the same pixels to be sampled.

        field : str, optional
            Field name of attribute to be used the label the extracted data
            Used only if the response feature represents a GeoDataframe

        return_array : bool, default = False
            Optionally return extracted data as separate X, y and xy
            masked numpy arrays
        
        duplicates : str, default = 'keep'
            Method to deal with duplicates points that fall inside the same
            pixel. Available options are ['keep', 'mean', min', 'max']

        na_rm : bool, default = True
            Optionally remove rows that contain nodata values

        low_memory : bool, default = False
            Optionally extract pixel values in using a slower but memory-safe
            method

        Returns
        -------
        gpd : geopandas GeoDataframe
            Containing extracted data as point geometries

        X : array-like
            Numpy masked array of extracted raster values, typically 2d
            Returned only if return_array is True

        y: 1d array like
            Numpy masked array of labelled sampled
            Returned only if return_array is True

        xy: 2d array-like
            Numpy masked array of row and column indexes of training pixels
            Returned only if return_array is True
        """

        if not field:
            y = None
        
        duplicate_methods = ['keep', 'mean', 'min', 'max']
        if duplicates not in duplicate_methods:
            raise ValueError('duplicates must be one of ' + str(duplicate_methods))

        # polygon and line geometries
        if all(response.geom_type == 'Polygon') or all(response.geom_type == 'LineString'):

            if all(response.geom_type == 'LineString'):
                all_touched = True
            else:
                all_touched = False

            rows_all, cols_all, y_all = [], [], []

            for i, shape in response.iterrows():

                if not field:
                    shapes = (shape.geometry, 1)
                else:
                    shapes = (shape.geometry, shape[field])

                arr = np.zeros((self.height, self.width))
                arr[:] = -99999
                arr = rasterio.features.rasterize(
                    shapes=(shapes for i in range(1)), fill=-99999, out=arr,
                    transform=self.transform, default_value=1,
                    all_touched=all_touched)

                rows, cols = np.nonzero(arr != -99999)

                if field:
                    y_all.append(arr[rows, cols])

                rows_all.append(rows)
                cols_all.append(cols)

            rows = list(chain.from_iterable(rows_all))
            cols = list(chain.from_iterable(cols_all))
            y = list(chain.from_iterable(y_all))

            xy = np.transpose(
                rasterio.transform.xy(transform=self.transform,
                                      rows=rows, cols=cols))

        # point geometries
        elif all(response.geom_type == 'Point'):
            xy = response.bounds.iloc[:, 2:].values
            if field:
                y = response[field].values

            # clip points to extent of raster
            xy, y = self._clip_xy(xy, y)
            rows, cols = rasterio.transform.rowcol(
                transform=self.transform, xs=xy[:, 0], ys=xy[:, 1])
            
            # deal with duplicate points that fall inside same pixel
            if duplicates != "keep":
                rowcol_df = pd.DataFrame(
                    np.column_stack((rows, cols, y)),
                    columns=['row', 'col'] + field)
                rowcol_df['Duplicated'] = rowcol_df.loc[:, ['row', 'col']].duplicated()
        
                if duplicates == 'mean':
                    rowcol_df = rowcol_df.groupby(by=['Duplicated', 'row', 'col'], sort=False).mean().reset_index()
                elif duplicates == 'min':
                    rowcol_df = rowcol_df.groupby(by=['Duplicated', 'row', 'col'], sort=False).min().reset_index()
                elif duplicates == 'max':
                    rowcol_df = rowcol_df.groupby(by=['Duplicated', 'row', 'col'], sort=False).max().reset_index()
        
                rows, cols = rowcol_df['row'].values, rowcol_df['col'].values
                y = rowcol_df[field].values

        # spatial query of Raster object (by-band)
        if low_memory is False:
            X = self._extract_by_indices(rows, cols)
        else:
            X = self.extract_xy(xy)

        # mask nodata values
        ## flatten masks for X and broadcast for two bands (x & y)
        mask_2d = X.mask.any(axis=1).repeat(2).reshape((X.shape[0], 2))

        ## apply mask to y values and spatial coords only if na_rm is True
        ## otherwise we want to get the y values and coords back even if some of the
        ## X values include nans
        if field and na_rm is True:
            y = np.ma.masked_array(y, mask=X.mask.any(axis=1))
            xy = np.ma.masked_array(xy, mask=mask_2d)

        # optionally remove rows containing nodata
        if na_rm is True:
            mask = X.mask.any(axis=1)
            X = np.ma.getdata(X)
            if field:
                y = np.ma.masked_array(data=y, mask=mask)
                y = np.ma.getdata(y)
            xy = np.ma.getdata(xy)

        # return as geopandas array as default (or numpy arrays)
        if return_array is False:
            if field is not None:
                data = np.ma.column_stack((y, X))
                column_names = [field] + self.names
            else:
                data = X
                column_names = self.names

            gdf = pd.DataFrame(data, columns=column_names)
            gdf['geometry'] = list(zip(xy[:, 0], xy[:, 1]))
            gdf['geometry'] = gdf['geometry'].apply(Point)
            gdf = gpd.GeoDataFrame(gdf, geometry='geometry', crs=self.crs)
            return gdf
        else:
            return X, y, xy

    def extract_raster(self, response, value_name='value', return_array=False,
                       na_rm=True):
        """
        Sample a Raster object by an aligned raster of labelled pixels

        Args
        ----
        response: rasterio.io.DatasetReader
            Single band raster containing labelled pixels

        return_array : bool, default = False
            Optionally return extracted data as separate X, y and xy
            masked numpy arrays

        na_rm : bool, default = True
            Optionally remove rows that contain nodata values

        Returns
        -------
        gpd : geopandas GeoDataFrame
            Geodataframe containing extracted data as point features

        X : array-like
            Numpy masked array of extracted raster values, typically 2d

        y: 1d array like
            Numpy masked array of labelled sampled

        xy: 2d array-like
            Numpy masked array of row and column indexes of training pixels
        """

        # open response raster and get labelled pixel indices and values
        arr = response.read(1, masked=True)
        rows, cols = np.nonzero(~arr.mask)
        xy = np.transpose(rasterio.transform.xy(response.transform, rows, cols))
        y = arr.data[rows, cols]

        # extract Raster object values at row, col indices
        X = self._extract_by_indices(rows, cols)

        # summarize data and mask nodatavals in X, y, and xy
        mask_2d = X.mask.any(axis=1).repeat(2).reshape((X.shape[0], 2))
        y = np.ma.masked_array(y, mask=X.mask.any(axis=1))
        xy = np.ma.masked_array(xy, mask=mask_2d)

        if na_rm is True:
            mask = X.mask.any(axis=1)
            X = X[~mask].data
            y = y[~mask].data
            xy = xy[~mask].data

        if return_array is False:
            column_names = [value_name] + self.names
            gdf = pd.DataFrame(np.ma.column_stack((y, X)), columns=column_names)
            gdf['geometry'] = list(zip(xy[:, 0], xy[:, 1]))
            gdf['geometry'] = gdf['geometry'].apply(Point)
            gdf = gpd.GeoDataFrame(gdf, geometry='geometry', crs=self.crs)
            return gdf
        else:
            return X, y, xy

def _predfun(img, estimator):
    """
    Prediction function for classification or regression response

    Args
    ----
    img : 3d numpy array of raster data

    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    Returns
    -------
    result_cla : 2d numpy array
        Single band raster as a 2d numpy array containing the
        classification or regression result
    """

    n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]

    # reshape each image block matrix into a 2D matrix
    # first reorder into rows,cols,bands(transpose)
    # then resample into 2D array (rows=sample_n, cols=band_values)
    n_samples = rows * cols
    flat_pixels = img.transpose(1, 2, 0).reshape(
        (n_samples, n_features))

    # create mask for NaN values and replace with number
    flat_pixels_mask = flat_pixels.mask.copy()

    # prediction
    result_cla = estimator.predict(flat_pixels)

    # replace mask
    result_cla = np.ma.masked_array(
        data=result_cla, mask=flat_pixels_mask.any(axis=1))

    # reshape the prediction from a 1D into 3D array [band, row, col]
    result_cla = result_cla.reshape((1, rows, cols))

    return result_cla


def _probfun(img, estimator):
    """
    Class probabilities function

    Args
    ----
    img : 3d numpy array of raster data [band, row, col]

    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    Returns
    -------
    result_proba : 3d numpy array
        Multi band raster as a 3d numpy array containing the
        probabilities associated with each class.
        Array is in (class, row, col) order
    """

    n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]

    mask2d = img.mask.any(axis=0)

    # reshape each image block matrix into a 2D matrix
    # first reorder into rows,cols,bands(transpose)
    # then resample into 2D array (rows=sample_n, cols=band_values)
    n_samples = rows * cols
    flat_pixels = img.transpose(1, 2, 0).reshape(
        (n_samples, n_features))

    # predict probabilities
    result_proba = estimator.predict_proba(flat_pixels)

    # reshape class probabilities back to 3D image [iclass, rows, cols]
    result_proba = result_proba.reshape(
        (rows, cols, result_proba.shape[1]))

    # reshape band into rasterio format [band, row, col]
    result_proba = result_proba.transpose(2, 0, 1)

    # repeat mask for n_bands
    mask3d = np.repeat(a=mask2d[np.newaxis, :, :], repeats=result_proba.shape[0], axis=0)

    # convert proba to masked array
    result_proba = np.ma.masked_array(
        result_proba,
        mask=mask3d,
        fill_value=np.nan)

    return result_proba


def _maximum_dtype(src):
    """
    Returns a single dtype that is large enough to store data
    within all raster bands

    Args
    ----
    src : rasterio.io.DatasetReader
        Rasterio datasetreader in the opened mode

    Returns
    -------
    dtype : str
        Dtype that is sufficiently large to store all raster
        bands in a single numpy array
    """

    if 'complex128' in src.dtypes:
        dtype = 'complex128'
    elif 'complex64' in src.dtypes:
        dtype = 'complex64'
    elif 'complex' in src.dtypes:
        dtype = 'complex'
    elif 'float64' in src.dtypes:
        dtype = 'float64'
    elif 'float32' in src.dtypes:
        dtype = 'float32'
    elif 'int32' in src.dtypes:
        dtype = 'int32'
    elif 'uint32' in src.dtypes:
        dtype = 'uint32'
    elif 'int16' in src.dtypes:
        dtype = 'int16'
    elif 'uint16' in src.dtypes:
        dtype = 'uint16'
    elif 'uint16' in src.dtypes:
        dtype = 'uint16'
    elif 'bool' in src.dtypes:
        dtype = 'bool'

    return dtype


def predict(estimator, dataset, file_path=None, predict_type='raw',
            indexes=None, driver='GTiff', dtype='float32', nodata=-99999):
    """
    Apply prediction of a scikit learn model to a GDAL-supported
    raster dataset

    Args
    ----
    estimator : estimator object implementing 'fit'
        The object to use to fit the data

    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader

    file_path : str, optional
        Path to a GeoTiff raster for the classification results
        If not supplied then output is written to a temporary file

    predict_type : str, optional (default='raw')
        'raw' for classification/regression
        'prob' for probabilities

    indexes : List, int, optional
        List of class indices to export

    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export

    dtype : str, optional. Default is 'float32'
        Numpy data type for file export

    nodata : any number, optional. Default is -99999
        Nodata value for file export

    Returns
    -------
    rasterio.io.DatasetReader with predicted raster
    """

    src = dataset

    # chose prediction function
    if predict_type == 'raw':
        predfun = _predfun
    elif predict_type == 'prob':
        predfun = _probfun

    # determine output count
    if predict_type == 'prob' and isinstance(indexes, int):
        indexes = range(indexes, indexes+1)

    elif predict_type == 'prob' and indexes is None:
        img = src.read(masked=True, window=(0, 0, 1, src.width))
        n_features, rows, cols = img.shape[0], img.shape[1], img.shape[2]
        n_samples = rows * cols
        flat_pixels = img.transpose(1, 2, 0).reshape(
            (n_samples, n_features))
        result = estimator.predict_proba(flat_pixels)
        indexes = range(result.shape[0])

    elif predict_type == 'raw':
        indexes = range(1)

    # open output file with updated metadata
    meta = src.meta
    meta.update(driver=driver, count=len(indexes), dtype=dtype, nodata=nodata)

    # optionally output to a temporary file
    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    with rasterio.open(file_path, 'w', **meta) as dst:

        # define windows
        windows = [window for ij, window in dst.block_windows()]

        # generator gets raster arrays for each window
        # read all bands if single dtype
        if src.dtypes.count(src.dtypes[0]) == len(src.dtypes):
            data_gen = (src.read(window=window, masked=True)
                        for window in windows)

        # else read each band separately
        else:
            def read(src, window):
                dtype = _maximum_dtype(src)
                arr = np.ma.zeros((src.count, window.height, window.width),
                                  dtype=dtype)

                for band in range(src.count):
                    arr[band, :, :] = src.read(
                        band+1, window=window, masked=True)

                return arr

            data_gen = (read(src=src, window=window) for window in windows)

        with tqdm(total=len(windows)) as pbar:
            for window, arr in zip(windows, data_gen):
                result = predfun(arr, estimator)
                result = np.ma.filled(result, fill_value=nodata)
                dst.write(result[indexes, :, :].astype(dtype), window=window)
                pbar.update(1)

    return rasterio.open(file_path)


def calc(dataset, function, file_path=None, driver='GTiff', dtype='float32',
         nodata=-99999):
    """
    Apply prediction of a scikit learn model to a GDAL-supported
    raster dataset

    Args
    ----
    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader

    function : function that takes an numpy array as a single argument

    file_path : str, optional
        Path to a GeoTiff raster for the classification results
        If not supplied then output is written to a temporary file

    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export

    dtype : str, optional. Default is 'float32'
        Numpy data type for file export

    nodata : any number, optional. Default is -99999
        Nodata value for file export

    Returns
    -------
    rasterio.io.DatasetReader containing result of function output
    """

    src = dataset

    # determine output dimensions
    img = src.read(masked=True, window=(0, 0, 1, src.width))
    arr = function(img)
    if len(arr.shape) > 2:
        indexes = range(arr.shape[0])
    else:
        indexes = 1

    # optionally output to a temporary file
    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    # open output file with updated metadata
    meta = src.meta
    meta.update(driver=driver, count=len(indexes), dtype=dtype, nodata=nodata)

    with rasterio.open(file_path, 'w', **meta) as dst:

        # define windows
        windows = [window for ij, window in dst.block_windows()]

        # generator gets raster arrays for each window
        # read all bands if single dtype
        if src.dtypes.count(src.dtypes[0]) == len(src.dtypes):
            data_gen = (src.read(window=window, masked=True)
                        for window in windows)

        # else read each band separately
        else:
            def read(src, window):
                dtype = _maximum_dtype(src)
                arr = np.ma.zeros((src.count, window.height, window.width),
                                  dtype=dtype)

                for band in range(src.count):
                    arr[band, :, :] = src.read(
                        band+1, window=window, masked=True)

                return arr

            data_gen = (read(src=src, window=window) for window in windows)

        with tqdm(total=len(windows)) as pbar:

            for window, arr in zip(windows, data_gen):
                result = function(arr)
                result = np.ma.filled(result, fill_value=nodata)
                dst.write(result.astype(dtype), window=window)
                pbar.update(1)

    return rasterio.open(file_path)


def crop(dataset, bounds, file_path=None, driver='GTiff'):
    """
    Crops a rasterio dataset by the supplied bounds

    dataset : rasterio.io.DatasetReader
        An opened Rasterio DatasetReader

    bounds : tuple
        A tuple containing the bounding box to clip by in the
        form of (xmin, xmax, ymin, ymax)

    file_path : str, optional. Default=None
        File path to save to cropped raster.
        If not supplied then the cropped raster is saved to a
        temporary file

    driver : str, optional. Default is 'GTiff'
        Named of GDAL-supported driver for file export

    Returns
    -------
    rasterio.io.DatasetReader with the cropped raster
    """

    src = dataset

    xmin, xmax, ymin, ymax = bounds

    rows, cols = rasterio.transform.rowcol(
        src.transform, xs=(xmin, xmax), ys=(ymin, ymax))

    cropped_arr = src.read(window=Window(col_off=min(cols),
                                         row_off=min(rows),
                                         width=max(cols) - min(cols),
                                         height=max(rows) - min(rows)))

    meta = src.meta
    aff = src.transform
    meta['width'] = max(cols) - min(cols)
    meta['height'] = max(rows) - min(rows)
    meta['transform'] = Affine(aff.a, aff.b, xmin, aff.d, aff.e, ymin)
    meta['driver'] = driver

    if file_path is None:
        file_path = tempfile.NamedTemporaryFile().name

    with rasterio.open(file_path, 'w', **meta) as dst:
        dst.write(cropped_arr)

    return rasterio.open(file_path)
