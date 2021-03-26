def geojson(result):
    properties = {
        "label": str(result),
    }
    if result._scores:
        properties["score"] = result.score
    for key in result.keys:
        val = getattr(result, key, None)
        # if val and key not in ['lat', 'lon', '_id']:
        if val and key not in ['lat', 'lon']:
            properties[key] = val
    type_ = result._doc.get('type')
    if type_ and type_ not in properties:
        properties[type_] = properties.get('name')
    housenumber = getattr(result, 'housenumber', None)
    if housenumber:
        if result.lang == 'zh':
            properties['name'] = '{}{}號'.format(properties.get('name'),
                                                housenumber)
        else:
            properties['name'] = '{} {}'.format(housenumber,
                                                properties.get('name'))
    try:
        properties['distance'] = int(result.distance)
    except ValueError:
        pass
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [float(result.lon), float(result.lat)]
        },
        "properties": properties,
    }
