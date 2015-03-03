''' Provider that returns PostGIS vector tiles in GeoJSON or MVT format.

VecTiles is intended for rendering, and returns tiles with contents simplified,
precision reduced and often clipped. The MVT format in particular is designed
for use in Mapnik with the VecTiles Datasource, which can read binary MVT tiles.

For a more general implementation, try the Vector provider:
    http://tilestache.org/doc/#vector-provider
'''
from math import pi
from urlparse import urljoin, urlparse
from urllib import urlopen
from os.path import exists
from shapely.wkb import loads

import json
from ... import getTile
from ...Core import KnownUnknown

try:
    from psycopg2.extras import RealDictCursor
    from psycopg2 import connect
    from psycopg2.extensions import TransactionRollbackError

except ImportError, err:
    # Still possible to build the documentation without psycopg2

    def connect(*args, **kwargs):
        raise err

from . import mvt, geojson, topojson, oscimap, mapbox
from ...Geography import SphericalMercator
from ModestMaps.Core import Point

tolerances = [6378137 * 2 * pi / (2 ** (zoom + 8)) for zoom in range(22)]

class Provider:
    ''' VecTiles provider for PostGIS data sources.
    
        Parameters:
        
          dbinfo:
            Required dictionary of Postgres connection parameters. Should
            include some combination of 'host', 'user', 'password', and 'database'.
        
          queries:
            Required list of Postgres queries, one for each zoom level. The
            last query in the list is repeated for higher zoom levels, and null
            queries indicate an empty response.
            
            Query must use "__geometry__" for a column name, and must be in
            spherical mercator (900913) projection. A query may include an
            "__id__" column, which will be used as a feature ID in GeoJSON
            instead of a dynamically-generated hash of the geometry. A query
            can additionally be a file name or URL, interpreted relative to
            the location of the TileStache config file.
            
            If the query contains the token "!bbox!", it will be replaced with
            a constant bounding box geomtry like this:
            "ST_SetSRID(ST_MakeBox2D(ST_MakePoint(x, y), ST_MakePoint(x, y)), <srid>)"
            
            This behavior is modeled on Mapnik's similar bbox token feature:
            https://github.com/mapnik/mapnik/wiki/PostGIS#bbox-token
          
          clip:
            Optional boolean flag determines whether geometries are clipped to
            tile boundaries or returned in full. Default true: clip geometries.
        
          srid:
            Optional numeric SRID used by PostGIS for spherical mercator.
            Default 900913.
        
          simplify:
            Optional floating point number of pixels to simplify all geometries.
            Useful for creating double resolution (retina) tiles set to 0.5, or
            set to 0.0 to prevent any simplification. Default 1.0.
        
          simplify_until:
            Optional integer specifying a zoom level where no more geometry
            simplification should occur. Default 16.

          suppress_simplification:
            Optional list of zoom levels where no dynamic simplification should
            occur.

          geometry_types:
            Optional list of geometry types that constrains the results of what
            kind of features are returned.

        Sample configuration, for a layer with no results at zooms 0-9, basic
        
          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:Provider",
            "kwargs":
            {
              "dbinfo":
              {
                "host": "localhost",
                "user": "gis",
                "password": "gis",
                "database": "gis"
              },
              "table": "road_network",
              "geometries":
              [
                null, null, null, null, null,
                null, null, null, null, null,
                "way",
                "way",
                "way"
              ],
              "attributes": 
              [ "trim(name) as name", "length" ]
            }
          }
    '''
    def __init__(self, layer, dbinfo, table, geometries, attributes=[], clip=True, srid=900913, simplify=1.0, simplify_until=16, suppress_simplification=(), geometry_types=None):
        '''
        '''
        self.layer = layer
        
        keys = 'host', 'user', 'password', 'database', 'port', 'dbname'
        self.dbinfo = dict([(k, v) for (k, v) in dbinfo.items() if k in keys])

        self.clip = bool(clip)
        self.srid = int(srid)
        self.simplify = float(simplify)
        self.simplify_until = int(simplify_until)
        self.suppress_simplification = set(suppress_simplification)
        self.geometry_types = None if geometry_types is None else set(geometry_types)

        self.table = table
        self.geometries = []
        self.attributes = {}
        self.columns = {}
        
        for i,query in enumerate(geometries):
            if query is None:
                self.geometries.append(None)
                continue
            self.geometries.append(query)
            if attributes:
              if len(attributes) > 1:
                self.attributes[query] = attributes[i]
              else:
                self.attributes[query] = attributes[0]
        
    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        try:
            geometry = self.geometries[coord.zoom]
        except IndexError:
            geometry = self.geometries[-1]
        if not geometry:
            return EmptyResponse(bounds)

        attributes = self.attributes.get(geometry, [])

        if coord.zoom in self.suppress_simplification:
            tolerance = None
        else:
            tolerance = self.simplify * tolerances[coord.zoom] if coord.zoom < self.simplify_until else None

        ll = self.layer.projection.coordinateProj(coord.down())
        ur = self.layer.projection.coordinateProj(coord.right())
        bounds = ll.x, ll.y, ur.x, ur.y
        
        if geometry not in self.columns:
            self.columns[geometry] = query_columns(self.dbinfo, self.srid, self.table, geometry, attributes, bounds)
        columns = self.columns[geometry]
        

        return Response(self.dbinfo, self.srid, self.table, geometry, attributes, columns, bounds, tolerance, coord.zoom, self.clip, coord, self.layer.name(), self.geometry_types)

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, one of "mvt", "json" or "topojson".
        '''
        if extension.lower() == 'mvt':
            return 'application/octet-stream+mvt', 'MVT'
        
        elif extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png

        elif extension.lower() == 'mapbox':
            return 'application/x-protobuf', 'Mapbox'

        else:
            raise ValueError(extension + " is not a valid extension")

class MultiProvider:
    ''' VecTiles provider to gather PostGIS tiles into a single multi-response.
        
        Returns a MultiResponse object for GeoJSON or TopoJSON requests.
    
        names:
          List of names of vector-generating layers from elsewhere in config.
        
        Sample configuration, for a layer with combined data from water
        and land areas, both assumed to be vector-returning layers:
        
          "provider":
          {
            "class": "TileStache.Goodies.VecTiles:MultiProvider",
            "kwargs":
            {
              "names": ["water-areas", "land-areas"]
            }
          }
    '''
    def __init__(self, layer, names, ignore_cached_sublayers=False):
        self.layer = layer
        self.names = names
        self.ignore_cached_sublayers = ignore_cached_sublayers
    
    def __call__(self, layer, names, ignore_cached_sublayers=False):
        self.layer = layer
        self.names = names
        self.ignore_cached_sublayers = ignore_cached_sublayers

    def renderTile(self, width, height, srs, coord):
        ''' Render a single tile, return a Response instance.
        '''
        return MultiResponse(self.layer.config, self.names, coord, self.ignore_cached_sublayers)

    def getTypeByExtension(self, extension):
        ''' Get mime-type and format by file extension, "json" or "topojson" only.
        '''
        if extension.lower() == 'json':
            return 'application/json', 'JSON'
        
        elif extension.lower() == 'topojson':
            return 'application/json', 'TopoJSON'

        elif extension.lower() == 'vtm':
            return 'image/png', 'OpenScienceMap' # TODO: make this proper stream type, app only seems to work with png
        
        elif extension.lower() == 'mapbox':
            return 'application/x-protobuf', 'Mapbox'

        else:
            raise ValueError(extension + " is not a valid extension for responses with multiple layers")

class Connection:
    ''' Context manager for Postgres connections.
    
        See http://www.python.org/dev/peps/pep-0343/
        and http://effbot.org/zone/python-with-statement.htm
    '''
    def __init__(self, dbinfo):
        self.dbinfo = dbinfo
    
    def __enter__(self):
        self.db = connect(**self.dbinfo).cursor(cursor_factory=RealDictCursor)
        return self.db
    
    def __exit__(self, type, value, traceback):
        self.db.connection.close()

class Response:
    '''
    '''
    def __init__(self, dbinfo, srid, table, geometry, attributes, columns, bounds, tolerance, zoom, clip, coord, layer_name, geometry_types):
        ''' Create a new response object with Postgres connection info and a query.
        
            bounds argument is a 4-tuple with (xmin, ymin, xmax, ymax).
        '''
        self.dbinfo = dbinfo
        self.table = table
        self.geometry = geometry
        self.bounds = bounds
        self.zoom = zoom
        self.clip = clip
        self.coord= coord
        self.layer_name = layer_name
        self.geometry_types = geometry_types
        
        geo_query = build_query(srid, table, geometry, attributes, columns, bounds, tolerance, True, clip)
        merc_query = build_query(srid, table, geometry, attributes, columns, bounds, tolerance, False, clip)
        oscimap_query = build_query(srid, table, geometry, attributes, columns, bounds, tolerance, False, clip, oscimap.padding * tolerances[coord.zoom], oscimap.extents)
        mapbox_query = build_query(srid, table, geometry, attributes, columns, bounds, tolerance, False, clip, mapbox.padding * tolerances[coord.zoom], mapbox.extents)
        self.query = dict(TopoJSON=geo_query, JSON=geo_query, MVT=merc_query, OpenScienceMap=oscimap_query, Mapbox=mapbox_query)

    def save(self, out, format):
        '''
        '''
        features = get_features(self.dbinfo, self.query[format], self.geometry_types)

        if format == 'MVT':
            mvt.encode(out, features)
        
        elif format == 'JSON':
            geojson.encode(out, features, self.zoom, self.clip)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, features, (ll.lon, ll.lat, ur.lon, ur.lat), self.clip)

        elif format == 'OpenScienceMap':
            oscimap.encode(out, features, self.coord, self.layer_name)

        elif format == 'Mapbox':
            mapbox.encode(out, features, self.coord, self.layer_name)

        else:
            raise ValueError(format + " is not supported")

class EmptyResponse:
    ''' Simple empty response renders valid MVT or GeoJSON with no features.
    '''
    def __init__(self, bounds):
        self.bounds = bounds
    
    def save(self, out, format):
        '''
        '''
        if format == 'MVT':
            mvt.encode(out, [])
        
        elif format == 'JSON':
            geojson.encode(out, [], 0, False)
        
        elif format == 'TopoJSON':
            ll = SphericalMercator().projLocation(Point(*self.bounds[0:2]))
            ur = SphericalMercator().projLocation(Point(*self.bounds[2:4]))
            topojson.encode(out, [], (ll.lon, ll.lat, ur.lon, ur.lat), False)

        elif format == 'OpenScienceMap':
            oscimap.encode(out, [], None)

        elif format == 'Mapbox':
            mapbox.encode(out, [], None)

        else:
            raise ValueError(format + " is not supported")

class MultiResponse:
    '''
    '''
    def __init__(self, config, names, coord, ignore_cached_sublayers):
        ''' Create a new response object with TileStache config and layer names.
        '''
        self.config = config
        self.names = names
        self.coord = coord
        self.ignore_cached_sublayers = ignore_cached_sublayers

    def save(self, out, format):
        '''
        '''
        if format == 'TopoJSON':
            topojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)
        
        elif format == 'JSON':
            geojson.merge(out, self.names, self.get_tiles(format), self.config, self.coord)

        elif format == 'OpenScienceMap':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["OpenScienceMap"], layer.provider.geometry_types)})
            oscimap.merge(out, feature_layers, self.coord)
        
        elif format == 'Mapbox':
            feature_layers = []
            layers = [self.config.layers[name] for name in self.names]
            for layer in layers:
                width, height = layer.dim, layer.dim
                tile = layer.provider.renderTile(width, height, layer.projection.srs, self.coord)
                if isinstance(tile,EmptyResponse): continue
                feature_layers.append({'name': layer.name(), 'features': get_features(tile.dbinfo, tile.query["Mapbox"], layer.provider.geometry_types)})
            mapbox.merge(out, feature_layers, self.coord)

        else:
            raise ValueError(format + " is not supported for responses with multiple layers")

    def get_tiles(self, format):
        unknown_layers = set(self.names) - set(self.config.layers.keys())
    
        if unknown_layers:
            raise KnownUnknown("%s.get_tiles didn't recognize %s when trying to load %s." % (__name__, ', '.join(unknown_layers), ', '.join(self.names)))
        
        layers = [self.config.layers[name] for name in self.names]
        mimes, bodies = zip(*[getTile(layer, self.coord, format.lower(), self.ignore_cached_sublayers, self.ignore_cached_sublayers) for layer in layers])
        bad_mimes = [(name, mime) for (mime, name) in zip(mimes, self.names) if not mime.endswith('/json')]
        
        if bad_mimes:
            raise KnownUnknown('%s.get_tiles encountered a non-JSON mime-type in %s sub-layer: "%s"' % ((__name__, ) + bad_mimes[0]))
        
        tiles = map(json.loads, bodies)
        bad_types = [(name, topo['type']) for (topo, name) in zip(tiles, self.names) if topo['type'] != ('FeatureCollection' if (format.lower()=='json') else 'Topology')]
        
        if bad_types:
            raise KnownUnknown('%s.get_tiles encountered a non-%sCollection type in %s sub-layer: "%s"' % ((__name__, ('Feature' if (format.lower()=='json') else 'Topology'), ) + bad_types[0]))
        
        return tiles


def query_columns(dbinfo, srid, table, geometry, attributes, bounds):
    ''' Get information about the columns returned for a given geometry and attribute query.
    '''
    with Connection(dbinfo) as db:
        bbox = 'ST_MakeBox2D(ST_MakePoint(%f, %f), ST_MakePoint(%f, %f))' % bounds
        bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)

        columns = ', '.join([geometry + ' AS __geometry__'] + attributes)
        query = "SELECT %s FROM %s" % (columns, table)
        query = query.replace('!bbox!', bbox)

        # newline is important here, to break out of comments.
        db.execute(query + '\n LIMIT 0')
        column_names = set(x.name for x in db.description)
        return column_names

def get_features(dbinfo, query, geometry_types, n_try=1):
    features = []

    with Connection(dbinfo) as db:
        try:
            db.execute(query)
        except TransactionRollbackError:
            if n_try >= 5:
                print 'TransactionRollbackError occurred 5 times'
                raise
            else:
                return get_features(dbinfo, query, geometry_types,
                                    n_try=n_try + 1)
        for row in db.fetchall():
            assert '__geometry__' in row, 'Missing __geometry__ in feature result'
            assert '__id__' in row, 'Missing __id__ in feature result'

            wkb = bytes(row.pop('__geometry__'))
            id = row.pop('__id__')

            if geometry_types is not None:
                shape = loads(wkb)
                geom_type = shape.__geo_interface__['type']
                if geom_type not in geometry_types:
                    #print 'found %s which is not in: %s' % (geom_type, geometry_types)
                    continue

            props = dict((k, v) for k, v in row.items() if v is not None)
            features.append((wkb, props, id))

    return features

def build_query(srid, table, geometry, attributes, columns, bounds, tolerance, is_geo, is_clipped, padding=0, scale=None):
    ''' Build and return an PostGIS query.
    '''
    bbox = 'ST_MakeBox2D(ST_MakePoint(%.12f, %.12f), ST_MakePoint(%.12f, %.12f))' % (bounds[0] - padding, bounds[1] - padding, bounds[2] + padding, bounds[3] + padding)
    bbox = 'ST_SetSRID(%s, %d)' % (bbox, srid)

    geom = geometry 
    
    # the order of the next two operations may have a performance impact
    # this order preserves clean tile borders, that cannot be distorted from simplification 
    if tolerance is not None:
        geom = 'ST_SimplifyPreserveTopology(%s, %.12f)' % (geom, tolerance)

    if is_clipped:
        geom = 'ST_Intersection(%s, %s)' % (geom, bbox)
    
    if is_geo:
        geom = 'ST_Transform(%s, 4326)' % geom

    if scale:
      # scale applies to the un-padded bounds, e.g. geometry in the padding area "spills over" past the scale range
      geom = ('ST_TransScale(%s, %.12f, %.12f, %.12f, %.12f)'
                % (geom, -bounds[0], -bounds[1],
                   scale / (bounds[2] - bounds[0]),
                   scale / (bounds[3] - bounds[1])))

    if '__geometry__' not in columns:
        raise Exception("There's supposed to be a __geometry__ column. We got %s" % ",".join(columns))

    if '__id__' not in columns:
        attributes.append('Substr(MD5(ST_AsBinary(%s)), 1, 10) AS __id__' % geometry)
        columns.add("__id__")

    attributes = ', '.join(attributes)
    
    return '''SELECT %(attributes)s,
                     ST_AsBinary(%(geom)s) AS __geometry__
              FROM %(table)s 
              WHERE %(geometry)s && %(bbox)s''' \
            % locals()
