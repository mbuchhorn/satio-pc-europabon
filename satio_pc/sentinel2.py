import atexit
import tempfile
import numpy as np
import xarray as xr
import dask.array as da
from loguru import logger

from satio_pc.preprocessing.composite import calculate_moving_composite
from satio_pc.preprocessing.interpolate import interpolate_ts_linear
from satio_pc.preprocessing.rescale import rescale_ts
from satio_pc.preprocessing.timer import FeaturesTimer
from satio_pc.indices import rsi_ts
from satio_pc.features import percentile


def mask_clouds(darr, scl_mask):
    """darr has dims (time, band, y, x),
    mask has dims (time, y, x)"""
    mask = da.broadcast_to(scl_mask.data, darr.shape)
    darr_masked = da.where(~mask, 0, darr)
    return darr.copy(data=darr_masked)


def force_unique_time(darr):
    """Add microseconds to time vars which repeats in order to make the
    time index of the DataArray unique, as sometimes observations from the same
    day can be split in multiple obs"""
    unique_ts, counts_ts = np.unique(darr.time, return_counts=True)
    double_ts = unique_ts[np.where(counts_ts > 1)]

    new_time = []
    c = 0
    for i in range(darr.time.size):
        v = darr.time[i].values
        if v in double_ts:
            v = v + c
            c += 1
        new_time.append(v)
    new_time = np.array(new_time)
    darr['time'] = new_time
    return darr


def harmonize(data):
    """
    Harmonize new Sentinel-2 data to the old baseline.

    https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a#Baseline-Change
    https://github.com/microsoft/PlanetaryComputer/issues/134

    Parameters
    ----------
    data: xarray.DataArray
        A DataArray with four dimensions: time, band, y, x

    Returns
    -------
    harmonized: xarray.DataArray
        A DataArray with all values harmonized to the old
        processing baseline.
    """
    baseline = data.coords['s2:processing_baseline'].astype(float)
    baseline_flag = baseline < 4

    if all(baseline_flag):
        return data

    offset = 1000
    bands = ["B01", "B02", "B03", "B04",
             "B05", "B06", "B07", "B08",
             "B8A", "B09", "B10", "B11", "B12"]

    old = data.isel(time=baseline_flag)
    to_process = list(set(bands) & set(data.band.data.tolist()))

    new = data.sel(time=~baseline_flag).drop_sel(band=to_process)

    new_harmonized = data.sel(time=~baseline_flag, band=to_process).copy()

    new_harmonized = new_harmonized.clip(offset)
    new_harmonized -= offset

    new = xr.concat([new, new_harmonized], "band").sel(
        band=data.band.data.tolist())
    return xr.concat([old, new], dim="time")


def load_sentinel2_tile(tile,
                        start_date,
                        end_date,
                        max_cloud_cover=90):
    import pystac_client
    import planetary_computer
    import stackstac

    catalog = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        modifier=planetary_computer.sign_inplace,
    )

    time_range = f"{start_date}/{end_date}"

    query_params = {"eo:cloud_cover": {"lt": max_cloud_cover},
                    "s2:mgrs_tile": {"eq": tile}}

    search = catalog.search(collections=["sentinel-2-l2a"],
                            datetime=time_range,
                            query=query_params)
    items = search.get_all_items()

    assets_10m = ['B02', 'B03', 'B04', 'B08']
    assets_20m = ['B05', 'B06', 'B07', 'B8A', 'B09',
                  'B11', 'B12']
    assets_60m = ['B01', 'B09']
    scl = 'SCL'

    ds = {}
    assets = {10: assets_10m,
              20: assets_20m,
              60: assets_60m,
              'scl': [scl]}

    chunksize = {10: 1024,
                 20: 512,
                 60: 512,
                 'scl': 512}

    dtype = {10: np.uint16,
             20: np.uint16,
             60: np.uint16,
             'scl': np.uint8}

    keep_vars = ['time', 'band', 'y', 'x', 'id', 's2:processing_baseline']
    for res in assets.keys():
        ds[res] = stackstac.stack(items,
                                  assets=assets[res],
                                  chunksize=chunksize[res],
                                  xy_coords='center',
                                  rescale=False,
                                  dtype=dtype[res],
                                  fill_value=0)
        del ds[res].attrs['spec']
        ds_vars = list(ds[res].coords.keys())
        drop_vars = [v for v in ds_vars if v not in keep_vars]
        ds[res] = ds[res].drop_vars(drop_vars)
        ds[res] = force_unique_time(ds[res])

        # coerce dtypes
        for v in ['id', 'band', 's2:processing_baseline']:
            ds[res][v] = ds[res][v].astype(str)

    return ds[10], ds[20], ds[60], ds['scl']


@xr.register_dataarray_accessor("satio")
class ESAWorldCoverTimeSeries:
    def __init__(self, xarray_obj):
        self._obj = xarray_obj
        self._obj.attrs['bounds'] = self.bounds
        # run check that we have a timeseries
        # assert xarray_obj.dims == ('time', 'band', 'y', 'x')

    def rescale(self,
                scale=2,
                order=1,
                preserve_range=True,
                nodata_value=0):
        return rescale_ts(self._obj,
                          scale=scale,
                          order=order,
                          preserve_range=preserve_range,
                          nodata_value=nodata_value)

    def mask(self, mask):
        return mask_clouds(self._obj, mask)

    def composite(self,
                  freq=7,
                  window=None,
                  start=None,
                  end=None,
                  use_all_obs=False):
        return calculate_moving_composite(self._obj,
                                          freq,
                                          window,
                                          start,
                                          end,
                                          use_all_obs)

    def interpolate(self):
        darr_interp = da.map_blocks(
            interpolate_ts_linear,
            self._obj.data,
            dtype=self._obj.dtype,
            chunks=self._obj.chunks)

        out = self._obj.copy(data=darr_interp)
        return out

    def s2indices(self, indices, clip=True):
        """Compute Sentinel-2 remote sensing indices"""
        return rsi_ts(self._obj, indices, clip)

    def percentile(self, q=[10, 25, 50, 75, 90]):
        """Compute set of percentiles for the time-series bands"""
        return percentile(self._obj, q)

    @property
    def bounds(self):

        darr = self._obj

        res = darr.x[1] - darr.x[0]
        hres = res / 2

        xmin = (darr.x[0] - hres).values.tolist()
        xmax = (darr.x[-1] + hres).values.tolist()

        ymin = (darr.y[-1] - hres).values.tolist()
        ymax = (darr.y[0] + hres).values.tolist()

        return xmin, ymin, xmax, ymax

    def harmonize(self):
        return harmonize(self._obj)

    def cache(self, tempdir='.', chunks=(-1, -1, 256, 256)):
        tmpfile = tempfile.NamedTemporaryFile(suffix='.nc',
                                              prefix='satio-',
                                              dir=tempdir)

        chunks = self._obj.chunks if chunks is None else chunks

        self._obj.to_netcdf(tmpfile.name)
        darr = xr.open_dataarray(tmpfile.name).chunk(chunks)

        atexit.register(tmpfile.close)
        return darr

    def rgb(self, bands=None, vmin=0, vmax=1000, **kwargs):
        import hvplot.xarray  # noqa
        import hvplot.pandas  # noqa
        import panel as pn  # noqa
        import panel.widgets as pnw

        bands = ['B04', 'B03', 'B02'] if bands is None else bands
        im = self._obj.sel(band=bands).clip(vmin, vmax) / (vmax - vmin)
        return im.interactive.sel(
            time=pnw.DiscreteSlider).hvplot.rgb(
                x='x', y='y',
            bands='band',
            data_aspect=1,
            xaxis=None,
            yaxis=None,
            **kwargs)

    def plot(self, band=None, vmin=None, vmax=None,
             colormap='plasma', **kwargs):
        import hvplot.xarray  # noqa
        import hvplot.pandas  # noqa
        import panel as pn  # noqa
        import panel.widgets as pnw

        im = self._obj
        band = im.band[0] if band is None else band
        im = im.sel(band=band)
        return im.interactive.sel(time=pnw.DiscreteSlider).plot(
            vmin=vmin,
            vmax=vmax,
            colormap=colormap,
            **kwargs)


def preprocess_s2(ds10_block,
                  ds20_block,
                  scl20_block,
                  start_date,
                  end_date,
                  composite_freq=10,
                  composite_window=20,
                  reflectance=True,
                  tmpdir='.'):

    timer10 = FeaturesTimer(10)
    timer20 = FeaturesTimer(20)

    with tempfile.TemporaryDirectory(prefix='satio_tmp-', dir=tmpdir) as tmpdirname:

        # download
        logger.info("Loading block data")
        timer10.load.start()
        ds10_block = ds10_block.satio.cache(tmpdirname)
        timer10.load.stop()

        timer20.load.start()
        ds20_block = ds20_block.satio.cache(tmpdirname)
        scl20_block = scl20_block.satio.cache(tmpdirname)
        scl10_block = scl20_block.satio.rescale(scale=2,
                                                order=0)
        scl10_block = scl10_block.satio.cache(tmpdirname)
        timer20.load.stop()

        # 10m
        # mask clouds
        timer10.composite.start()
        ds10_block_masked = ds10_block.satio.mask(
            scl10_block).satio.cache(tmpdirname)

        logger.info("Compositing 10m block data")
        # composite
        ds10_block_comp = ds10_block_masked.satio.composite(
            freq=composite_freq,
            window=composite_window,
            start=start_date,
            end=end_date).satio.cache(tmpdirname)
        timer10.composite.stop()

        logger.info("Interpolating 10m block data")
        # interpolation
        timer10.interpolate.start()
        ds10_block_interp = ds10_block_comp.satio.interpolate(
        ).satio.cache(tmpdirname)
        timer10.interpolate.stop()

        # 20m
        # mask
        timer20.composite.start()
        ds20_block_masked = ds20_block.satio.mask(
            scl20_block).satio.cache(tmpdirname)

        logger.info("Compositing 20m block data")
        # composite
        ds20_block_comp = ds20_block_masked.satio.composite(
            freq=composite_freq,
            window=composite_window,
            start=start_date,
            end=end_date).satio.cache(tmpdirname)
        timer20.composite.stop()

        logger.info("Interpolating 20m block data")
        # interpolation
        timer20.interpolate.start()
        ds20_block_interp = ds20_block_comp.satio.interpolate(
        ).satio.cache(tmpdirname)
        timer20.interpolate.stop()

        logger.info("Merging 10m and 20m series")
        # merging to 10m cleaned data
        ds20_block_interp_10m = ds20_block_interp.satio.rescale(scale=2,
                                                                order=1,
                                                                nodata_value=0)
        dsm10 = xr.concat([ds10_block_interp,
                           ds20_block_interp_10m],
                          dim='band')

        if reflectance:
            dsm10 = dsm10.astype(np.float32) / 10000

        dsm10.attrs = ds10_block.attrs

        for t in timer10, timer20:
            t.load.log()
            t.composite.log()
            t.interpolate.log()

    dsm10 = dsm10.satio.cache()

    return dsm10
