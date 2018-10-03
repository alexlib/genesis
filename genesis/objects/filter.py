"""
Use 2D mask or object tracking to filter out objects and create a new objects
file
"""
import os
import warnings

import xarray as xr
import numpy as np
import tqdm

import cloud_identification

import cloud_tracking_analysis
from cloud_tracking_analysis import CloudData
from cloud_tracking_analysis.tracking_utility import TrackingType


def label_objects(mask, splitting_scalar=None, remove_at_edge=True):
    def _remove_at_edge(object_labels):
        mask_edge = np.zeros_like(mask)
        mask_edge[:,:,1] = True  # has to be k==1 because object identification codes treats actual edges as ghost cells
        mask_edge[:,:,-2] = True
        cloud_identification.remove_intersecting(object_labels, mask_edge)

    if splitting_scalar is None:
        splitting_scalar = np.ones_like(mask)
    else:
        assert mask.shape == splitting_scalar.shape
        assert mask.dims == splitting_scalar.dims
        # NB: should check coord values too

    object_labels = cloud_identification.number_objects(
        splitting_scalar, mask=mask
    )

    if remove_at_edge:
        _remove_at_edge(object_labels)

    return object_labels


def filter_objects_by_mask(objects, mask):
    if mask.dims != objects.dims:
        if mask.dims == ('yt', 'xt'):
            assert objects.dims[:2] == mask.dims
            # mask_3d = np.zeros(objects.shape, dtype=bool)
            _, _, nz = objects.shape
            # XXX: this is pretty disguisting, there must be a better way...
            # inspired from https://stackoverflow.com/a/44151668
            mask_3d = np.moveaxis(np.repeat(mask.values[None, :], nz, axis=0), 0, 2)
        else:
            raise Exception(mask.dims)
    else:
        mask_3d = mask

    cloud_identification.remove_intersecting(objects, ~mask_3d)

    return objects

def filter_objects_by_tracking(objects, base_name, dt_pad):
    t0 = objects.time.values
    valid_units = ["seconds since 2000-01-01 00:00:00", "seconds since 2000-01-01"]
    assert objects.time.units in valid_units
    t_min, t_max = t0 - dt_pad, t0 + dt_pad
    tracking_identifier = '{}s-{}s'.format(t_min, t_max)

    def get_dataset_name_and_path(input_name):
        dataset_name = input_name.split('/')[-1].split('.')[0]
        dataset_path = input_name.split('/')[0]

        return dataset_name, dataset_path

    dataset_name, dataset_path = get_dataset_name_and_path(objects.input_name)

    # ensure cloud-tracking analysis is loading files relative to current path
    CloudData.set_root_path(os.getcwd())
    cloud_data = cloud_tracking_analysis.CloudData(
        dataset_name=dataset_name,
        dataset_pathname=dataset_path,
        tracking_identifier=tracking_identifier,
        tracking_type=TrackingType.THERMALS_ONLY
    )

    ds_track_2d = cloud_data._fh_track.sel(time=objects.time)
    objects_tracked_2d = ds_track_2d.nrthrm

    # project 3D objects
    objects_projected_2d = objects.where(objects > 1, other=0).max(dim='zt')
    objects_projected_2d.name = 'objects_projected_2d'

    # create mask which only includes regions which were tracked
    m = np.logical_and(~np.isnan(objects_tracked_2d), objects_projected_2d != 0)

    # mask out projected ids
    projected_labels_tracked = objects_projected_2d.where(m, other=np.nan)
    projected_labels_tracked.name = 'projected_labels_tracked'

    # find out which object ids exist in this projected region
    filter_nans = lambda v: v[~np.isnan(v)]

    id3d_tracked_from_projected = filter_nans(np.unique(projected_labels_tracked))

    objects_filtered = np.zeros_like(objects)

    print("Picking out objects which were tracked...")
    for object_id in tqdm.tqdm(id3d_tracked_from_projected):
        objects_filtered += objects.where(objects == object_id, other=0)

    return objects_filtered


if __name__ == "__main__":
    import argparse
    argparser = argparse.ArgumentParser(__doc__)

    argparser.add_argument('object_file', type=str)

    subparsers = argparser.add_subparsers(dest="subparser_name")

    arg_group_mask = subparsers.add_parser('mask')
    arg_group_mask.add_argument('mask-name', type=str)
    arg_group_mask.add_argument('--mask-field', default=None, type=str)

    arg_group_tracking = subparsers.add_parser('tracking')
    arg_group_tracking.add_argument('--dt-pad', default=20*60, type=float)

    args = argparser.parse_args()

    object_file = args.object_file.replace('.nc', '')

    if not 'objects' in object_file:
        raise Exception()

    base_name, objects_mask = object_file.split('.objects.')

    fn_objects = "{}.nc".format(object_file)
    if not os.path.exists(fn_objects):
        raise Exception("Couldn't find objects file `{}`".format(fn_objects))
    objects = xr.open_dataarray(fn_objects, decode_times=False)

    if args.subparser_name == "mask":
        fn_mask = "{}.{}.mask.nc".format(base_name, args.mask_name)
        if not os.path.exists(fn_mask):
            raise Exception("Couldn't find mask file `{}`".format(fn_mask))

        if args.mask_field is None:
            mask_field = args.mask_name
        else:
            mask_field = args.mask_field
        mask_description = mask_field

        ds_mask = xr.open_dataset(fn_mask, decode_times=False)
        if not mask_field in ds_mask:
            raise Exception("Can't find `{}` in mask, loaded mask file:\n{}"
                            "".format(mask_field, str(ds_mask)))
        else:
            mask = ds_mask[mask_field]

        ds = filter_objects_by_mask(objects=objects, mask=mask)

        ds.attrs['input_name'] = object_file
        ds.attrs['mask_name'] = "{} && {}".format(ds.mask_name, mask_description)

        out_filename = "{}.objects.{}.{}.nc".format(
            base_name.replace('/', '__'), objects_mask, mask_description
        )
    elif args.subparser_name == "tracking":
        ds = filter_objects_by_tracking(objects=objects, base_name=base_name,
                                        dt_pad=args.dt_pad)
        ds.attrs['input_name'] = object_file
        ds.attrs['mask_name'] = "tracked__dt_pad{}s".format(args.dt_pad)

        out_filename = "{}.objects.{}.{}.nc".format(
            base_name.replace('/', '__'), objects_mask, "tracked"
        )
    else:
        raise NotImplementedError

    ds.to_netcdf(out_filename)
    print("Wrote output to `{}`".format(out_filename))
