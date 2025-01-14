import json
import logging
import logging.handlers
from pathlib import Path
import time
import geohash

import falcon

from addok.core import reverse, search
from addok.helpers.text import EntityTooLarge
from addok.config import config
from hashids import Hashids

notfound_logger = None
query_logger = None
slow_query_logger = None

hashids = Hashids()


def get_logger(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    filename = Path(config.LOG_DIR).joinpath('{}.log'.format(name))
    try:
        handler = logging.handlers.TimedRotatingFileHandler(
                                            str(filename), when='midnight')
    except FileNotFoundError:
        print('Unable to write to {}'.format(filename))
    else:
        logger.addHandler(handler)
    return logger


@config.on_load
def on_load():
    if config.LOG_NOT_FOUND:
        global notfound_logger
        notfound_logger = get_logger('notfound')

    if config.LOG_QUERIES:
        global query_logger
        query_logger = get_logger('queries')

    if config.SLOW_QUERIES:
        global slow_query_logger
        slow_query_logger = get_logger('slow_queries')


def log_notfound(query):
    if config.LOG_NOT_FOUND:
        notfound_logger.debug(query)


def log_query(query, results):
    if config.LOG_QUERIES:
        if results:
            result = str(results[0])
            score = str(round(results[0].score, 2))
        else:
            result = '-'
            score = '-'
        query_logger.debug('\t'.join([query, result, score]))


def log_slow_query(query, results, timer):
    if config.SLOW_QUERIES:
        if results:
            result = str(results[0])
            score = str(round(results[0].score, 2))
            id_ = results[0].id
        else:
            result = '-'
            score = '-'
            id_ = '-'
        slow_query_logger.debug('\t'.join([str(timer),
                                           query, id_, result, score]))


class CorsMiddleware:

    def process_response(self, req, resp, resource):
        resp.set_header('Access-Control-Allow-Origin', '*')
        resp.set_header('Access-Control-Allow-Headers', 'X-Requested-With')


class View:

    config = config

    def match_filters(self, req):
        filters = {}
        for name in config.FILTERS:
            req.get_param(name, store=filters)
        return filters

    def render(self, req, resp, results, query=None, filters=None, center=None,
               limit=None):
        results = {
            "type": "FeatureCollection",
            "version": "draft",
            "features": [r.format() for r in results],
            "attribution": config.ATTRIBUTION,
            "licence": config.LICENCE,
        }
        if query:
            results['query'] = query
        if filters:
            results['filters'] = filters
        if center:
            results['center'] = center
        if limit:
            results['limit'] = limit
        self.json(req, resp, results)

    to_geojson = render  # retrocompat.

    def json(self, req, resp, content):
        resp.body = json.dumps(content)
        resp.content_type = 'application/json; charset=utf-8'

    def parse_lon_lat(self, req):
        try:
            lat = float(req.get_param('lat'))
            for key in ('lon', 'lng', 'long'):
                lon = req.get_param(key)
                if lon is not None:
                    lon = float(lon)
                    break
        except (ValueError, TypeError):
            lat = None
            lon = None
        return lon, lat


class Search(View):

    def on_get(self, req, resp, **kwargs):
        query = req.get_param('q')
        language = req.get_param('language') or 'zh'
        if not query:
            raise falcon.HTTPBadRequest('Missing query', 'Missing query')
        limit = req.get_param_as_int('limit') or 20  # use config
        autocomplete = req.get_param_as_bool('autocomplete')
        if autocomplete is None:
            # Default is True.
            # https://github.com/falconry/falcon/pull/493#discussion_r44376219
            autocomplete = True
        lon, lat = self.parse_lon_lat(req)
        center = None
        if lon and lat:
            center = (lon, lat)
        filters = self.match_filters(req)
        timer = time.perf_counter()
        try:
            results = search(query, limit=limit, autocomplete=autocomplete,
                             lat=lat, lon=lon, **filters)
        except EntityTooLarge as e:
            raise falcon.HTTPRequestEntityTooLarge(str(e))
        timer = int((time.perf_counter()-timer)*1000)
        if not results:
            log_notfound(query)
        log_query(query, results)
        if config.SLOW_QUERIES and timer > config.SLOW_QUERIES:
            log_slow_query(query, results, timer)
        filtered_results = {}

        def sortbyindex(item):
            idx = item.name.lower().find(query.lower())
            if idx == -1:
                idx = 100
            return idx

        def sortbylang(item):
            if item.lang == language:
                return 0
            else:
                return 1

        def sortbylength(item):
            return len(item.name)

        results.sort(key=lambda x: (sortbylang(x), sortbyindex(x), sortbylength(x)))
        count = 0
        for r in results:
            if not filtered_results.get(r.name.lower()) and r.type[0] != 'R':
                filtered_results[r.name.lower()] = r
                count += 1
            elif not filtered_results.get(r.id) and r.type[0] == 'R':
                filtered_results[r.id] = r
                count += 1
            if not req.get_param_as_int('limit') and count == 5:
                break
        
        self.render(req, resp, list(filtered_results.values()), query=query, filters=filters,
                    center=center, limit=limit)


class Reverse(View):

    def on_get(self, req, resp, **kwargs):
        lon, lat = self.parse_lon_lat(req)
        placeID = req.get_param('place_id')
        if (lon is None or lat is None) and not placeID:
            raise falcon.HTTPBadRequest('Invalid args', 'Invalid args')
        placeIDArray = []
        if placeID:
            placeIDArray = placeID.split('_')
            if len(placeIDArray) > 1:
                lat, lon = geohash.decode(placeIDArray[-1][:12])
        limit = req.get_param_as_int('limit') or 5
        filters = self.match_filters(req)
        if len(placeIDArray) > 1 and placeIDArray[-1] and len(placeIDArray[-1]) > 12:
            filters['type'] = placeIDArray[-1][12:].upper()
            try:
                filters['id'] = str(hashids.decode(placeIDArray[0])[0])
            except:
                filters['id'] = '_'.join(placeIDArray[:-1])
            if filters['type'] == 'H':
                filters['type'] = 'housenumber'
        results = reverse(lat=lat, lon=lon, limit=limit, **filters)
        tieredResults = [[],[],[],[],[],[]]
        for result in results:
            if result.type[0] == 'R' and not tieredResults[4]:
                tieredResults[5].append(result)
            elif result.distance < 10:
                if result.type in ['B', 'S', 'SS']:
                    tieredResults[0].append(result)
                else:
                    tieredResults[1].append(result)
            elif result.type != 'ST':
                if result.type in ['B', 'S', 'SS']:
                    tieredResults[2].append(result)
                else:
                    tieredResults[3].append(result)
            else:
                tieredResults[4].append(result)

        finalResults = []
        for tr in tieredResults:
            finalResults += tr

        self.render(req, resp, finalResults[:1], filters=filters, limit=limit)


def register_http_endpoint(api):
    api.add_route('/search', Search())
    api.add_route('/reverse', Reverse())


def register_command(subparsers):
    parser = subparsers.add_parser('serve', help='Run debug server')
    parser.set_defaults(func=run)
    parser.add_argument('--host', default='127.0.0.1',
                        help='Host to expose the demo serve on')
    parser.add_argument('--port', default='7878',
                        help='Port to expose the demo server on')


def run(args):
    # Do not import at load time for preventing config import loop.
    from .wsgi import simple
    simple(args)
