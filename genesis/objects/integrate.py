import os
import warnings

import xarray as xr
import numpy as np
# forget about using dask for now, dask_ndmeasure takes a huge amount of memory
# try:
    # # raise ImportError
    # # import dask_ndmeasure as ndimage
    # # register a progressbar so we can see progress of dask'ed operations with xarray
    # from dask.diagnostics import ProgressBar
    # ProgressBar().register()
# except ImportError:
    # from scipy import ndimage
    # warnings.warn("Using standard serial scipy implementation instead of "
                  # "dask'ed dask-ndmeasure. Install `dask-ndmeasure` for much "
                  # "faster computation")

from scipy import ndimage
from tqdm import tqdm

from . import integral_properties

def _estimate_dx(da):
    dx = np.max(np.diff(da.xt))
    dy = np.max(np.diff(da.yt))
    dz = np.max(np.diff(da.zt))

    if not dx == dy == dz:
        raise Exception("{} != {} != {}".format(dx, dy, dz))

    return dx


def integrate(objects, da, operator):
    if 'object_ids' in da.coords:
        object_ids = da.object_ids
    else:
        # print("Finding unique values")
        object_ids = np.unique(objects.chunk(None).values)
        # ensure object 0 (outside objects) is excluded
        if object_ids[0] == 0:
            object_ids = object_ids[1:]

    if len(da.dims) == 1 and len(objects.dims) == 3:
        # special case for allowing integration of coordinates
        da = xr.broadcast(objects, da)[1]
    else:
        assert objects.dims == da.dims
        assert objects.shape == da.shape

    dx = _estimate_dx(da=da)

    if operator == "volume_integral":
        fn = ndimage.sum
        s = dx**3.0
        operator_units = 'm^3'
    else:
        fn = getattr(ndimage, operator)
        s = 1.0
        operator_units = ''

    vals = fn(da, labels=objects.values, index=object_ids)
    if hasattr(vals, 'compute'):
        vals = vals.compute()

    vals *= s

    longname = "per-object {} of {}".format(operator.replace('_', ' '), da.name)
    units = ("{} {}".format(da.units, operator_units)).strip()
    da = xr.DataArray(vals, coords=dict(object_id=object_ids),
                      dims=('object_id',),
                      attrs=dict(longname=longname, units=units),
                      name='{}__{}'.format(da.name, operator))

    return da


def _integrate_per_object(da_objects, fn_int):
    if 'object_ids' in da_objects.coords:
        object_ids = da_objects.object_ids
    else:
        # print("Finding unique values")
        object_ids = np.unique(objects.chunk(None).values)
        # ensure object 0 (outside objects) is excluded
        if object_ids[0] == 0:
            object_ids = object_ids[1:]

    if 'xt' in da_objects.coords:
        da_objects = da_objects.rename(dict(xt='x', yt='y', zt='z'))

    ds_per_object = []
    for object_id in tqdm(object_ids):
        da_object = da_objects.where(da_objects == object_id, drop=True)

        ds_object = fn_int(da_object)
        ds_object['object_id'] = object_id
        ds_per_object.append(ds_object)

    return xr.concat(ds_per_object, dim='object_id')


if __name__ == "__main__":
    import argparse

    argparser = argparse.ArgumentParser(__doc__)
    argparser.add_argument('object_file')
    argparser.add_argument('scalar_field')
    argparser.add_argument('--operator', default='volume_integral', type=str)

    args = argparser.parse_args()
    object_file = args.object_file.replace('.nc', '')

    op = args.operator

    chunks = 200  # forget about using dask for now, np.unique is too slow

    if not 'objects' in object_file:
        raise Exception()

    base_name, objects_mask = object_file.split('.objects.')

    fn_objects = "{}.nc".format(object_file)
    if not os.path.exists(fn_objects):
        raise Exception("Couldn't find objects file `{}`".format(fn_objects))
    objects = xr.open_dataarray(
        fn_objects, decode_times=False, chunks=chunks
    ).squeeze()

    ds_out = None

    scalar_field = args.scalar_field
    if scalar_field in objects.coords:
        da_scalar = objects.coords[args.scalar_field]
    elif scalar_field == 'com_angles':
        fn_int = integral_properties.calc_com_incline_and_orientation_angle
        ds_out = _integrate_per_object(da_objects=objects, fn_int=fn_int)
    elif scalar_field == 'volume':
        dx = _estimate_dx(objects)
        da_scalar = xr.DataArray(
            np.ones_like(objects, dtype=np.float)*dx**3.0,
            coords=objects.coords, attrs=dict(units='m^3')
        )
        da_scalar.name = 'volume'
    else:
        fn_scalar = "{}.{}.nc".format(base_name, args.scalar_field)
        if not os.path.exists(fn_scalar):
            raise Exception("Couldn't find scalar file `{}`".format(fn_scalar))

        da_scalar = xr.open_dataarray(
            fn_scalar, decode_times=False, chunks=chunks
        ).squeeze()

    if ds_out is None:
        if objects.zt.max() < da_scalar.zt.max():
            warnings.warn("Objects span smaller range than scalar field to "
                          "reducing domain of scalar field")
            zt_ = da_scalar.zt.values
            da_scalar = da_scalar.sel(zt=slice(None, zt_[25]))

        ds_out = integrate(objects=objects, da=da_scalar, operator=args.operator)

    out_filename = "{}.objects.{}.integral.{}.{}.nc".format(
        base_name.replace('/', '__'), objects_mask, scalar_field, op,
    )

    import ipdb
    with ipdb.launch_ipdb_on_exception():
        ds_out.to_netcdf(out_filename)
    print("Wrote output to `{}`".format(out_filename))
