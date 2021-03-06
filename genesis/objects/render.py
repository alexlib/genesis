# -*- coding: utf-8 -*-
import os
import xarray as xr
import numpy as np
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 unused import
import matplotlib.pyplot as plt

from ..utils import find_grid_spacing


def render_mask_as_3d_voxels(da_mask, ax=None, center_xy_pos=False, alpha=0.5):
    if ax is None:
        fig = plt.figure()
        ax = fig.gca(projection='3d')
    else:
        if not hasattr(ax, 'voxels'):
            raise Exception("The provided axes must have `projection='3d'` set")

    dx = dy = dz = find_grid_spacing(da_mask)

    dims = ['x', 'y', 'z']
    m_obj = da_mask.transpose(*dims).values

    x_c = da_mask[dims[0]]
    y_c = da_mask[dims[1]]
    z_c = da_mask[dims[2]]

    if center_xy_pos:
        x_c -= x_c.mean()
        y_c -= y_c.mean()

    x = np.empty(np.array(x_c.shape) + 1)
    y = np.empty(np.array(y_c.shape) + 1)
    z = np.empty(np.array(z_c.shape) + 1)

    x[:-1] = x_c - 0.5*dx
    y[:-1] = y_c - 0.5*dy
    z[:-1] = z_c - 0.5*dz
    x[-1] = x_c[-1] + 0.5*dx
    y[-1] = y_c[-1] + 0.5*dy
    z[-1] = z_c[-1] + 0.5*dz

    x, y, z = np.meshgrid(x, y, z, indexing='ij')

    m_edge = (
        (m_obj != np.roll(m_obj, axis=0, shift=1))
    |   (m_obj != np.roll(m_obj, axis=0, shift=-1))
    |   (m_obj != np.roll(m_obj, axis=1, shift=1))
    |   (m_obj != np.roll(m_obj, axis=1, shift=-1))
    |   (m_obj != np.roll(m_obj, axis=2, shift=1))
    |   (m_obj != np.roll(m_obj, axis=2, shift=-1))
    )

    colors = np.zeros(list(m_obj.shape) + [4,] , dtype=np.float32)
    colors[m_edge,0] = 0
    colors[m_edge,1] = 1
    colors[m_edge,2] = 0
    colors[m_edge,3] = alpha

    _ = ax.voxels(x, y, z, m_obj, facecolors=colors, edgecolors=[0, 0, 0, 0.5*alpha])
    ax.set_xlabel(xr.plot.utils.label_from_attrs(x_c))
    ax.set_ylabel(xr.plot.utils.label_from_attrs(y_c))
    ax.set_zlabel(xr.plot.utils.label_from_attrs(z_c))

    return ax



if __name__ == "__main__":
    import argparse
    argparser = argparse.ArgumentParser(description=__doc__)

    argparser.add_argument('object_file', type=str)
    argparser.add_argument('--object-id', type=int, required=True)
    argparser.add_argument('--center-xy-pos', action="store_true")

    args = argparser.parse_args()

    object_file = args.object_file.replace('.nc', '')

    if not 'objects' in object_file:
        raise Exception()

    base_name, objects_mask = object_file.split('.objects.')

    fn_objects = "{}.nc".format(object_file)
    if not os.path.exists(fn_objects):
        raise Exception("Couldn't find objects file `{}`".format(fn_objects))
    da_objects = xr.open_dataarray(fn_objects, decode_times=False)

    if not args.object_id in da_objects.values:
        raise Exception()

    obj_id = args.object_id
    da_obj = da_objects.where(da_objects == obj_id, drop=True)
    da_mask = da_obj == obj_id

    da_mask = da_mask.rename(dict(xt='x', yt='y', zt='z'))

    ax = render_mask_as_3d_voxels(da_mask=da_mask,
                                  center_xy_pos=args.center_xy_pos)

    plt.show()
