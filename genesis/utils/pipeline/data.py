import os
import subprocess
from pathlib import Path
import re
import warnings

import ipdb
import luigi
import xarray as xr
import numpy as np
import yaml

from .. import mask_functions, make_mask
from ... import objects
from ...bulk_statistics import cross_correlation_with_height
from ...utils import find_vertical_grid_spacing
from ...length_scales.minkowski import exponential_fit

import importlib

try:
    import cloud_tracking_analysis.cloud_data
    from cloud_tracking_analysis import CloudData, CloudType, cloud_operations
    HAS_CLOUD_TRACKING = True
except ImportError:
    HAS_CLOUD_TRACKING = False

def _get_dataset_meta_info(base_name):
    try:
        with open('datasources.yaml') as fh:
            loader = getattr(yaml, 'FullLoader', yaml.Loader)
            datasources = yaml.load(fh, Loader=loader)
    except IOError:
        raise Exception("please define your data sources in datasources.yaml")

    if datasources is None or not base_name in datasources:
        raise Exception("Please make a definition for `{}` in "
                        "datasources.yaml".format(base_name))

    return datasources[base_name]

class XArrayTarget(luigi.target.FileSystemTarget):
    fs = luigi.local_target.LocalFileSystem()

    def __init__(self, path, *args, **kwargs):
        super(XArrayTarget, self).__init__(path, *args, **kwargs)
        self.path = path

    def open(self, *args, **kwargs):
        ds = xr.open_dataset(self.path, *args, **kwargs)

        if len(ds.data_vars) == 1:
            name = list(ds.data_vars)[0]
            da = ds[name]
            da.name = name
            return da
        else:
            return ds

    @property
    def fn(self):
        return self.path

COMPOSITE_FIELD_METHODS = dict(
    p_stddivs=mask_functions.calc_scalar_perturbation_in_std_div,
)

class ExtractField3D(luigi.Task):
    base_name = luigi.Parameter()
    field_name = luigi.Parameter()

    FN_FORMAT = "{experiment_name}.{field_name}.nc"

    @staticmethod
    def _get_data_loader_module(meta):
        model_name = meta.get('model')
        if model_name is None:
            model_name = 'UCLALES'

        module_name = ".data_sources.{}".format(
            model_name.lower().replace('-', '_')
        )
        return importlib.import_module(module_name,
                                       package='genesis.utils.pipeline')

    def requires(self):
        meta = _get_dataset_meta_info(self.base_name)
        data_loader = self._get_data_loader_module(meta=meta)

        reqs = {}

        derived_fields = getattr(data_loader, 'DERIVED_FIELDS', None)

        if derived_fields is not None:
            for req_field in derived_fields.get(self.field_name, []):
                reqs[req_field] = ExtractField3D(base_name=self.base_name,
                                                 field_name=req_field)

        for (postfix, func) in COMPOSITE_FIELD_METHODS.items():
            if self.field_name.endswith(postfix):
                req_field = self.field_name.replace('_{}'.format(postfix), '')
                # XXX: the p_stddivs method takes a `da` arg for now because it
                # is general purpose
                reqs['da'] = ExtractField3D(base_name=self.base_name,
                                                 field_name=req_field)

        return reqs

    def run(self):
        meta = _get_dataset_meta_info(self.base_name)

        fn_out = self.output()

        if fn_out.exists():
            pass
        elif meta['host'] == 'localhost':
            p_out = Path(self.output().fn)
            p_out.parent.mkdir(parents=True, exist_ok=True)

            is_composite = False
            for (postfix, func) in COMPOSITE_FIELD_METHODS.items():
                if self.field_name.endswith(postfix):
                    das_input = dict([
                        (k, input.open(decode_times=False))
                        for (k, input) in self.input().items()
                    ])
                    da = func(**das_input)
                    # XXX: remove infs for now
                    da = da.where(~np.isinf(da))
                    da.to_netcdf(self.output().fn)
                    is_composite = True

            if not is_composite:
                data_loader = self._get_data_loader_module(meta=meta)
                data_loader.extract_field_to_filename(
                    dataset_meta=meta, path_out=p_out,
                    field_name=self.field_name,
                    **self.input()
                )
        else:
            raise NotImplementedError(fn_out.fn)

    def output(self):
        meta = _get_dataset_meta_info(self.base_name)

        fn = self.FN_FORMAT.format(
            experiment_name=meta['experiment_name'], timestep=meta['timestep'],
            field_name=self.field_name
        )

        p = Path("data")/self.base_name/fn

        return XArrayTarget(str(p))


class MakeMask(luigi.Task):
    base_name = luigi.Parameter()
    method_extra_args = luigi.Parameter(default='')
    method_name = luigi.Parameter()

    def requires(self):
        method_kwargs = self._build_method_kwargs()
        try:
            make_mask.build_method_kwargs(method=self.method_name, kwargs=method_kwargs)
        except make_mask.MissingInputException as e:
            return dict([
                (v, ExtractField3D(field_name=v, base_name=self.base_name))
                for v in e.missing_kwargs
            ])

    def _build_method_kwargs(self):
        kwargs = dict(base_name=self.base_name)
        for kv in self.method_extra_args.split(","):
            if kv == "":
                continue
            k,v = kv.split("=")
            kwargs[k] = v
        return kwargs

    def run(self):
        method_kwargs = self._build_method_kwargs()

        for (v, target) in self.input().items():
            method_kwargs[v] = xr.open_dataarray(target.fn, decode_times=False)

        cwd = os.getcwd()
        p_data = Path('data')/self.base_name
        os.chdir(p_data)
        mask = make_mask.main(method=self.method_name, method_kwargs=method_kwargs)
        os.chdir(cwd)
        mask.to_netcdf(self.output().fn)

    def output(self):
        kwargs = self._build_method_kwargs()

        try:
            kwargs = make_mask.build_method_kwargs(
                method=self.method_name, kwargs=kwargs
            )
        except make_mask.MissingInputException as e:
            for v in e.missing_kwargs:
                kwargs[v] = None
        kwargs = make_mask.build_method_kwargs(
            method=self.method_name, kwargs=kwargs
        )

        mask_name = make_mask.make_mask_name(
            method=self.method_name, method_kwargs=kwargs
        )
        fn = make_mask.OUT_FILENAME_FORMAT.format(
            base_name=self.base_name, mask_name=mask_name
        )
        p = Path('data')/self.base_name/fn
        return XArrayTarget(str(p))


class IdentifyObjects(luigi.Task):
    splitting_scalar = luigi.Parameter()
    base_name = luigi.Parameter()
    mask_method = luigi.Parameter()
    mask_method_extra_args = luigi.Parameter(default='')

    def requires(self):
        return dict(
            mask=MakeMask(
                base_name=self.base_name,
                method_name=self.mask_method,
                method_extra_args=self.mask_method_extra_args
            ),
            scalar=ExtractField3D(
                base_name=self.base_name,
                field_name=self.splitting_scalar
            )
        )

    def run(self):
        da_mask = xr.open_dataarray(self.input()['mask'].fn).squeeze()
        da_scalar = xr.open_dataarray(self.input()['scalar'].fn).squeeze()

        object_labels = objects.identify.process(
            mask=da_mask, splitting_scalar=da_scalar
        )

        object_labels.to_netcdf(self.output().fn)

    def output(self):
        if not self.input()["mask"].exists():
            return luigi.LocalTarget("fakefile.nc")

        da_mask = xr.open_dataarray(self.input()["mask"].fn, decode_times=False)
        mask_name = da_mask.name
        objects_name = objects.identify.make_objects_name(
            mask_name=mask_name, splitting_var=self.splitting_scalar
        )

        return luigi.LocalTarget(objects.identify.OUT_FILENAME_FORMAT.format(
            base_name=self.base_name, objects_name=objects_name
        ))

class ComputeObjectMinkowskiScales(luigi.Task):
    object_splitting_scalar = luigi.Parameter()
    base_name = luigi.Parameter()
    mask_method = luigi.Parameter()
    mask_method_extra_args = luigi.Parameter(default='')

    def requires(self):
        return IdentifyObjects(
            base_name=self.base_name,
            splitting_scalar=self.object_splitting_scalar,
            mask_method=self.mask_method,
            mask_method_extra_args=self.mask_method_extra_args,
        )

    def run(self):
        da_objects = xr.open_dataarray(self.input().fn)

        ds = objects.minkowski_scales.main(da_objects=da_objects)

        ds.to_netcdf(self.output().fn)

    def output(self):
        if not self.input().exists():
            return luigi.LocalTarget("fakefile.nc")

        da_objects = xr.open_dataarray(self.input().fn, decode_times=False)
        objects_name = da_objects.name

        fn = objects.minkowski_scales.FN_FORMAT.format(
            base_name=self.base_name, objects_name=objects_name
        )

        p = Path("data")/self.base_name/fn
        return XArrayTarget(str(p))

class ComputeObjectScale(luigi.Task):
    object_splitting_scalar = luigi.Parameter()
    base_name = luigi.Parameter()
    mask_method = luigi.Parameter()
    mask_method_extra_args = luigi.Parameter(default='')

    variable = luigi.Parameter()
    operator = luigi.Parameter(default='')

    def requires(self):
        return IdentifyObjects(
            base_name=self.base_name,
            splitting_scalar=self.object_splitting_scalar,
            mask_method=self.mask_method,
            mask_method_extra_args=self.mask_method_extra_args,
        )

    def output(self):
        if not self.input().exists():
            return luigi.LocalTarget("fakefile.nc")

        da_objects = xr.open_dataarray(self.input().fn, decode_times=False)
        objects_name = da_objects.name

        name = objects.integrate.make_name(variable=self.variable,
                                           operator=self.operator)

        fn = objects.integrate.FN_OUT_FORMAT.format(
            base_name=self.base_name, objects_name=objects_name,
            name=name
        )
        p = Path("data")/self.base_name/fn
        return XArrayTarget(str(p))

    def run(self):
        da_objects = xr.open_dataarray(self.input().fn)

        ds = objects.integrate.integrate(objects=da_objects,
                                         variable=self.variable)
        ds.to_netcdf(self.output().fn)


class ComputeObjectScales(luigi.Task):
    object_splitting_scalar = luigi.Parameter()
    base_name = luigi.Parameter()
    mask_method = luigi.Parameter()
    mask_method_extra_args = luigi.Parameter(default='')
    variables = luigi.Parameter(default='com_angles')

    def requires(self):
        variables = set(self.variables.split(','))
        reqs = []

        MINKOWSKI_VARS = "length width thickness".split(" ")

        if 'theta' in variables or 'phi' in variables:
            variables.remove('phi')
            variables.remove('theta')
            variables.add('com_incline_and_orientation_angle')

        for v in variables:
            if v in MINKOWSKI_VARS:
                reqs.append(
                    ComputeObjectMinkowskiScales(
                        base_name=self.base_name,
                        object_splitting_scalar=self.object_splitting_scalar,
                        mask_method=self.mask_method,
                        mask_method_extra_args=self.mask_method_extra_args,
                    )
                )
            else:
                reqs.append(
                    ComputeObjectScale(
                        base_name=self.base_name,
                        variable=v,
                        object_splitting_scalar=self.object_splitting_scalar,
                        mask_method=self.mask_method,
                        mask_method_extra_args=self.mask_method_extra_args,
                    )
                )

        return reqs

    def run(self):
        ds = xr.merge([
            input.open(decode_times=False) for input in self.input()
        ])

        ds.to_netcdf(self.output().fn)

    def output(self):
        if not self.input()[0].exists():
            return luigi.LocalTarget('fakename.nc')

        # XXX: ideally I'd use the mask method's filename generation method
        # here
        fn_scales0 = Path(self.input()[0].fn).name
        name = fn_scales0.split('.objects.')[-1]\
                         .replace('.minkowski_scales.nc', '')
        fn = '{}.{}.object_scales.nc'.format(
            self.base_name, name
        )
        p = Path("data")/self.base_name/fn
        target = XArrayTarget(str(p))

        if target.exists():
            ds = target.open(decode_times=False)
            variables = self.variables.split(',')
            if any([v not in ds.data_vars for v in variables]):
                p.unlink()

        return target


class ComputeCumulantProfiles(luigi.Task):
    pass


class ExtractCrossSection2D(luigi.Task):
    base_name = luigi.Parameter()
    field_name = luigi.Parameter()

    FN_FORMAT = "{exp_name}.out.xy.{field_name}.nc"


    def _extract_and_symlink_local_file(self):
        meta = _get_dataset_meta_info(self.base_name)

        p_out = Path(self.output().fn)
        p_in = Path(meta['path'])/"cross_sections"/"runtime_slices"/p_out.name

        p_out.parent.mkdir(exist_ok=True, parents=True)

        os.symlink(str(p_in), str(p_out))

    def output(self):
        meta = _get_dataset_meta_info(self.base_name)

        fn = self.FN_FORMAT.format(
            exp_name=meta['experiment_name'],
            field_name=self.field_name
        )

        p = Path("data")/self.base_name/"cross_sections"/"runtime_slices"/fn

        return luigi.LocalTarget(str(p))

    def run(self):
        meta = _get_dataset_meta_info(self.base_name)
        fn_out = self.output()

        if fn_out.exists():
            pass
        elif meta['host'] == 'localhost':
            self._extract_and_symlink_local_file()
        else:
            raise NotImplementedError(fn_out.fn)

class PerformObjectTracking2D(luigi.Task):
    base_name = luigi.Parameter()

    def requires(self):
        if not HAS_CLOUD_TRACKING:
            raise Exception("cloud_tracking_analysis module isn't available")

        return [
            ExtractCrossSection2D(base_name=self.base_name, field_name='core'),
            ExtractCrossSection2D(base_name=self.base_name, field_name='cldbase'),
            ExtractCrossSection2D(base_name=self.base_name, field_name='cldtop'),
            ExtractCrossSection2D(base_name=self.base_name, field_name='lwp'),
        ]

    def _get_tracking_identifier(self, meta):
        timestep_3d = meta['timestep']

        da_input = xr.open_dataarray(self.input()[0].fn, decode_times=False)
        # TODO: use more intelligent selection for timesteps to track here
        tn_max = len(da_input.time)

        return "track_1-{}".format(tn_max)

    def _get_dataset_name(self):
        meta = _get_dataset_meta_info(self.base_name)
        return meta['experiment_name']

    def get_cloud_data(self):
        meta = _get_dataset_meta_info(self.base_name)
        dataset_name = meta['experiment_name']

        tracking_identifier = self._get_tracking_identifier(meta)

        p_data = Path(os.getcwd())/"data"
        cloud_tracking_analysis.cloud_data.ROOT_DIR = str(p_data)
        cloud_data = CloudData(dataset_name, tracking_identifier,
                               dataset_pathname=self.base_name)

    def run(self):
        self.get_cloud_data()

    def output(self):
        if not all([i.exists() for i in self.input()]):
            return luigi.LocalTarget("fakefile.nc")

        meta = _get_dataset_meta_info(self.base_name)
        tracking_identifier = self._get_tracking_identifier(meta)
        tracking_identifier = tracking_identifier.replace('_', '__cloud_core__')

        dataset_name = meta['experiment_name']
        FN_2D_FORMAT = "{}.out.xy.{}.nc"

        fn = FN_2D_FORMAT.format(dataset_name, tracking_identifier)
        p = Path(os.getcwd())/"data"/self.base_name/"tracking_output"/fn
        return luigi.LocalTarget(str(p))


class ExtractCloudbaseState(luigi.Task):
    base_name = luigi.Parameter()
    field_name = luigi.Parameter()

    def requires(self):
        if HAS_CLOUD_TRACKING:
            return dict(
                tracking=PerformObjectTracking2D(base_name=self.base_name),
                field=ExtractField3D(base_name=self.base_name, field_name=self.field_name),
            )
        else:
            warnings.warn("cloud tracking isn't available. Using approximate"
                          " method for finding cloud-base height rather than"
                          "tracking")
            return dict(
                qc=ExtractField3D(base_name=self.base_name, field_name='qc'),
                field=ExtractField3D(base_name=self.base_name, field_name=self.field_name),
            )

    def run(self):
        if HAS_CLOUD_TRACKING:
            tracking_output = Path(self.input()["tracking"].fn)
            matches = re.match("(.*).out.xy.track__cloud_core__(.*).nc", tracking_output.name)
            dataset_name = matches[1]
            tracking_timerange = matches[2]
            tracking_identifier = "track_{}".format(tracking_timerange)

            p_data = Path(os.getcwd())/"data"
            cloud_tracking_analysis.cloud_data.ROOT_DIR = str(p_data)
            cloud_data = CloudData(dataset_name, tracking_identifier,
                                   dataset_pathname=self.base_name)

            da_scalar_3d = xr.open_dataarray(self.input()["field"].fn, decode_times=False)

            t0 = da_scalar_3d.time.values[0]
            z_cb = cross_correlation_with_height.get_cloudbase_height(
                cloud_data=cloud_data, t0=t0,
            )
            dz = cloud_data.dx
            method = 'tracked clouds'
        else:
            qc = self.input()['qc'].open()
            z_cb = cross_correlation_with_height.get_approximate_cloudbase_height(
                qc=qc, z_tol=50.
            )
            da_scalar_3d = self.input()['field'].open()
            try:
                dz = find_vertical_grid_spacing(da_scalar_3d)
                method = 'approximate'
            except:
                warnings.warn("Using cloud-base state because vertical grid"
                              " spacing is non-uniform")
                dz = 0.0
                method = 'approximate, in-cloud'

        da_cb = cross_correlation_with_height.extract_from_3d_at_heights_in_2d(
            da_3d=da_scalar_3d, z_2d=z_cb-dz
        )
        da_cb = da_cb.squeeze()
        da_cb.name = self.field_name
        da_cb.attrs['method'] = method

        da_cb.to_netcdf(self.output().fn)

    def output(self):
        fn = "{}.{}.cloudbase.xy.nc".format(self.base_name, self.field_name)
        p = Path('data')/self.base_name/fn
        return XArrayTarget(str(p))


class EstimateCharacteristicMinkowskiScales(luigi.Task):
    object_splitting_scalar = luigi.Parameter()
    base_name = luigi.Parameter()
    mask_method = luigi.Parameter()
    mask_method_extra_args = luigi.Parameter(default='')
    variables = ['length', 'width', 'thickness']

    def requires(self):
        return ComputeObjectScales(
            variables=",".join(self.variables), base_name=self.base_name,
            mask_method=self.mask_method,
            mask_method_extra_args=self.mask_method_extra_args,
            object_splitting_scalar=self.object_splitting_scalar,
        )

    def run(self):
        ds = self.input().open()
        ds_scales = ds[self.variables].apply(exponential_fit.fit)
        ds_scales.to_netcdf(self.output().fn)

    def output(self):
        fn_scales0 = Path(self.input().fn).name
        name = fn_scales0.split('.objects.')[-1]\
                         .replace('.minkowski_scales.nc', '')
        fn = '{}.{}.exp_fit_scales.nc'.format(
            self.base_name, name
        )
        p = Path("data")/self.base_name/fn
        target = XArrayTarget(str(p))
        return target