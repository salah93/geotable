import utm
from invisibleroads_macros.disk import (
    TemporaryStorage,
    compress,
    find_paths,
    get_file_stem,
    has_archive_extension,
    move_path,
    uncompress)
from invisibleroads_macros.exceptions import BadFormat
from invisibleroads_macros.html import make_random_color
from invisibleroads_macros.text import unicode_safely
from os.path import join
from osgeo import gdal, ogr, osr
from pandas import DataFrame, Series, concat, read_csv
from shapely.geometry import GeometryCollection

from .exceptions import GeoTableError
from .macros import (
    _get_field_definitions, _get_geometry_columns, _get_instance_for_csv,
    _get_instance_from_gdal_layer, _get_load_geometry_object,
    _get_proj4_from_path, _get_proj4_from_gdal_layer,
    _has_one_proj4)
from .projections import (
    _get_spatial_reference_from_proj4, _get_transform_gdal_geometry,
    get_transform_shapely_geometry, get_utm_proj4, normalize_proj4,
    LONLAT_PROJ4)


class GeoTable(DataFrame):

    @classmethod
    def load_utm_proj4(Class, source_path):
        geotable = Class.load(source_path, target_proj4=LONLAT_PROJ4)
        lonlat_point = GeometryCollection(geotable.geometries).centroid
        longitude, latitude = lonlat_point.x, lonlat_point.y
        zone_number, zone_letter = utm.from_latlon(latitude, longitude)[-2:]
        return get_utm_proj4(zone_number, zone_letter)

    @classmethod
    def load(Class, source_path, source_proj4=None, target_proj4=None, **kw):
        with TemporaryStorage() as storage:
            try:
                source_folder = uncompress(source_path, storage.folder)
            except BadFormat:
                if source_path.endswith('.shp'):
                    return Class.from_shp(
                        source_path, source_proj4, target_proj4)
                if source_path.endswith('.csv'):
                    return Class.from_csv(
                        source_path, source_proj4, target_proj4, **kw)
                raise GeoTableError(
                    'file format not supported (%s)' % source_path)
            try:
                return Class.from_shp(
                    source_folder, source_proj4, target_proj4)
            except GeoTableError:
                pass
            instances = []
            for x in find_paths(source_folder, '*.csv'):
                t = Class.from_csv(x, source_proj4, target_proj4, **kw)
                t['geometry_layer'] = unicode_safely(get_file_stem(x))
                instances.append(t)
            return concat(instances)

    @classmethod
    def from_shp(Class, source_path, source_proj4=None, target_proj4=None):
        try:
            gdal_dataset = gdal.OpenEx(source_path)
        except RuntimeError:
            raise GeoTableError('shapefile unloadable (%s)' % source_path)
        instances = []
        for layer_index in range(gdal_dataset.GetLayerCount()):
            gdal_layer = gdal_dataset.GetLayer(layer_index)
            row_proj4 = _get_proj4_from_gdal_layer(gdal_layer, source_proj4)
            transform_geometry = _get_transform_gdal_geometry(
                row_proj4, target_proj4)
            t = _get_instance_from_gdal_layer(
                Class, gdal_layer, transform_geometry)
            t['geometry_layer'] = unicode_safely(gdal_layer.GetName())
            t['geometry_proj4'] = normalize_proj4(target_proj4 or row_proj4)
            instances.append(t)
        return concat(instances)

    @classmethod
    def from_csv(
            Class, source_path, source_proj4=None, target_proj4=None, **kw):
        t = read_csv(source_path, **kw)
        try:
            geometry_columns = _get_geometry_columns(t)
        except GeoTableError as e:
            raise GeoTableError(str(e) + ' (%s)' % source_path)
        load_geometry_object = _get_load_geometry_object(geometry_columns)
        source_proj4 = _get_proj4_from_path(source_path, source_proj4)
        geometry_objects = []
        if _has_one_proj4(t):
            row_proj4 = t.iloc[0].get('geometry_proj4', source_proj4)
            transform_geometry = get_transform_shapely_geometry(
                row_proj4, target_proj4)
            for index, row in t.iterrows():
                geometry_objects.append(transform_geometry(
                    load_geometry_object(row)))
            t['geometry_proj4'] = normalize_proj4(target_proj4 or row_proj4)
        else:
            geometry_proj4s = []
            for index, row in t.iterrows():
                row_proj4 = row.get('geometry_proj4', source_proj4)
                transform_geometry = get_transform_shapely_geometry(
                    row_proj4, target_proj4)
                geometry_objects.append(transform_geometry(
                    load_geometry_object(row)))
                geometry_proj4s.append(normalize_proj4(
                    target_proj4 or row_proj4))
            t['geometry_proj4'] = geometry_proj4s
        t['geometry_object'] = geometry_objects
        return Class(t.drop(geometry_columns, axis=1))

    def to_shp(self, target_path, target_proj4=None):
        gdal_driver = gdal.GetDriverByName('ESRI Shapefile')
        if not gdal_driver:
            raise GeoTableError('shapefile driver missing')
        if not has_archive_extension(target_path):
            raise GeoTableError(
                'archive extension expected (%s)' % (target_path))
        field_names = self.field_names
        field_definitions = _get_field_definitions(self)
        with TemporaryStorage() as storage:
            gdal_dataset = gdal_driver.Create(storage.folder, 0, 0)
            for layer_name, a in self.groupby('geometry_layer'):
                layer_proj4 = target_proj4 or a.iloc[0]['geometry_proj4']
                gdal_layer = gdal_dataset.CreateLayer(
                    layer_name, _get_spatial_reference_from_proj4(layer_proj4))
                for field_definition in field_definitions:
                    gdal_layer.CreateField(field_definition)
                layer_definition = gdal_layer.GetLayerDefn()
                for source_proj4, b in a.groupby('geometry_proj4'):
                    transform_geometry = get_transform_shapely_geometry(
                        source_proj4, layer_proj4)
                    for index, row in b.iterrows():
                        ogr_feature = ogr.Feature(layer_definition)
                        for field_index, field_name in enumerate(field_names):
                            ogr_feature.SetField2(field_index, row[field_name])
                        ogr_feature.SetGeometry(ogr.CreateGeometryFromWkb(
                            transform_geometry(row['geometry_object']).wkb))
                        gdal_layer.CreateFeature(ogr_feature)
            gdal_dataset.FlushCache()
            compress(storage.folder, target_path)

    def to_csv(self, target_path, target_proj4=None, **kw):
        if 'index' not in kw:
            kw['index'] = False
        t = concat(_get_instance_for_csv(
            x, source_proj4 or LONLAT_PROJ4, target_proj4,
        ) for source_proj4, x in self.groupby('geometry_proj4'))
        with TemporaryStorage() as storage:
            temporary_path = join(storage.folder, 'geotable.csv')
            super(GeoTable, t).to_csv(temporary_path, **kw)
            if has_archive_extension(target_path):
                compress(storage.folder, target_path)
            else:
                move_path(target_path, temporary_path)

    def draw(self):
        return ColorfulGeometryCollection([GeometryCollection(
            x.geometries
        ) for _, x in self.groupby('geometry_layer')])

    @property
    def field_names(self):
        return [x for x in self.columns if x not in [
            'geometry_object', 'geometry_layer', 'geometry_proj4']]

    @property
    def geometries(self):
        return list(self['geometry_object'])

    @property
    def _constructor(self):
        return GeoTable

    @property
    def _constructor_sliced(self):
        return GeoRow


class GeoRow(Series):

    @property
    def _constructor(self):
        return GeoRow

    @property
    def _constructor_expanddim(self):
        return GeoTable


class ColorfulGeometryCollection(GeometryCollection):

    def __init__(self, geoms=None, colors=None):
        super(ColorfulGeometryCollection, self).__init__(geoms)
        self.colors = colors or [make_random_color() for x in range(len(
            geoms))]

    def svg(self, scale_factor=1.0, color=None):
        if self.is_empty:
            return '<g />'
        if not self.colors:
            return super(ColorfulGeometryCollection, self).svg(
                scale_factor, color)
        return '<g>%s</g>' % ''.join(p.svg(scale_factor, c) for p, c in zip(
            self, self.colors))


gdal.SetConfigOption('GDAL_NUM_THREADS', 'ALL_CPUS')
gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()
