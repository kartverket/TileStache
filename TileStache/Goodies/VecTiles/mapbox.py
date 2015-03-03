from TileStache.Core import KnownUnknown
import re
import logging

import mapbox_vector_tile

# coordindates are scaled to this range within tile
extents = 4096
#extents = 256

# tiles are padded by this number of pixels for the current zoom level 
padding = 5

def decode(file):
    tile = file.read()
    data = mapbox_vector_tile.decode(tile)
    return data # print data or write to file?

def encode(file, features, coord, layer_name=''):
    layers = []

    layers.append(get_feature_layer(layer_name, features))
    
    data = mapbox_vector_tile.encode(layers)
    file.write(data)

def merge(file, feature_layers, coord):
    ''' Retrieve a list of mapbox tile responses and merge them into one.
    
        get_tiles() retrieves data and performs basic integrity checks.
    '''
    layers = []
    
    for layer in feature_layers:
        layers.append(get_feature_layer(layer['name'], layer['features']))
        
    data = mapbox_vector_tile.encode(layers)
    file.write(data)

def get_feature_layer(name, features):
    _features = []

    for feature in features:
        if len(feature) >= 2:
            feature[1].update(uid=feature[2])
        _features.append({
            'geometry': feature[0],
            'properties': feature[1]
        })

    return {
        'name': name or '',
        'features': _features
    }
