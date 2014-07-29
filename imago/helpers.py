# Copyright (c) Sunlight Foundation, 2014
# Authors:
#    - Paul R. Tagliamonte <paultag@sunlightfoundation.com>
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Sunlight Foundation nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE SUNLIGHT FOUNDATION BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


from django.core.paginator import Paginator, EmptyPage
from restless.modelviews import ListEndpoint, DetailEndpoint
from restless.models import serialize
from restless.http import HttpError, Http200
from collections import defaultdict


def get_field_list(model, without=None):
    """
    Get a list of all known field names on a Django model. Optionally,
    you may exclude keys by passing a list of keys to avoid in the 'without'
    kwarg.
    """
    if without is None:
        without = set()
    else:
        without = set(without)
    return list(set(model._meta.get_all_field_names()) - without)


class FieldKeyError(KeyError):
    def __init__(self, field):
        self.field = field

    def __str__(self):
        return "<FieldKeyError: %s>" % (self.field)


def get_fields(root, fields):
    """
    Return a list of objects to prefetch and a composed spec for the
    DjangoRestless serialize call given a root spec dictionary and a list
    of fields.

    Fields may be dotted to represent sub-elements, which will
    traverse the root dictonary.

    This function returns a tuple, prefetch-able fields, and a serialize
    function spec. The result of the latter may be passed directly into
    serialize, and will limit based on `fields`, rather then `include` or
    `exclude`.
    """

    def fwrap(obj, memo=None):
        """
        Ensure this object can be passed into serialize by turning it from
        a raw structure dict into a serialize spec. Most of the time
        this is just wrapping dicts in {"fields": ...}.
        """
        memo = memo if memo else set()
        id_ = id(obj)
        if id_ in memo:
            return None
        memo.add(id_)

        if isinstance(obj, dict):
            if obj == {} or obj.get("fields"):
                return obj
            obj = list(filter(
                lambda x: x[1] != None,
                [(x, fwrap(y, memo=memo)) for x, y in obj.items()]
            ))
            if obj == []:
                return None
            return {"fields": obj}
        return obj

    prefetch = set([])
    subfields = defaultdict(list)
    concrete = []
    for field in fields:
        if '.' not in field:
            concrete.append(field)
            continue
        prefix, postfix = field.split(".", 1)
        subfields[prefix].append(postfix)

    try:
        ret = {x: fwrap(root[x]) for x in concrete}
    except KeyError as e:
        raise FieldKeyError(*e.args)

    for key, fields in subfields.items():
        prefetch.add(key)
        try:
            _prefetch, ret[key] = get_fields(root[key], fields)
        except FieldKeyError as e:
            e.field = "%s.%s" % (key, e.field)
            raise e
        prefetch = prefetch.union({"%s__%s" % (key, x) for x in _prefetch})

    return (prefetch, fwrap(ret))


def cachebusterable(fn):
    """
    Allow front-end tools to pass a "_" pararm with different arguments
    to work past cache. This is the default behavior for select2, and was
    easy enough to avoid.

    This ensures we don't get "_" in the view handler, avoding special
    casing in multiple places.
    """
    def _(self, request, *args, **kwargs):
        params = request.params
        if '_' in params:
            params.pop("_")
        return fn(self, request, *args, **kwargs)
    return _


class PublicListEndpoint(ListEndpoint):
    """
    Imago public list API helper class.

    This class exists to be subclassed by concrete views, and builds in
    sane default behavior for all list views.

    Critically it allows for:

         - Filtering
         - Sorting
         - Pagination
         - Meta-dictionary for the clients
         - Opinionated serializion with the helpers above.

    This allows our views to be declarative, and allow for subclass overriding
    of methods when needed.

    Access-Control-Allow-Origin is currently always set to "*", since this
    is a global read-only API.

    As a result, JSONP is disabled. Read more on using CORS:
       - http://en.wikipedia.org/wiki/Cross-origin_resource_sharing

    The 'get' class-based view method invokes the following helpers:


        [ Methods ]
         - get_query_set  | Get the Django query set for the request.
         - filter         | Filter the resulting query set.
         - sort           | Sort the filtered query set
         - paginate       | Paginate the sorted query set


        [ Object Properties ]
         - model            | Django ORM Model / class to query using.
         - per_page         | Objects to show per-page.

         - default_fields   | If no `fields` param is passed in, use this
                            | to limit the `serialize_config`.

         - serialize_config | Object serializion to use. Many are in
                            | the imago.serialize module
    """

    methods = ['GET']
    per_page = 100
    serialize_config = {}
    default_fields = []

    def adjust_filters(self, params):
        """
        Adjust the filter params from within the `filter' call.
        """
        return params

    def filter(self, data, **kwargs):
        """
        Filter the Django query set.

        THe kwargs will be unpacked into Django directly, letting you
        use full Django query syntax here.
        """
        kwargs = self.adjust_filters(kwargs)
        return data.filter(**kwargs)

    def sort(self, data, sort_by):
        """
        Sort the Django query set. The sort_by param will be
        unpacked into 'order_by' directly.
        """
        return data.order_by(*sort_by)

    def paginate(self, data, page):
        """
        Paginate the Django response. It will default to
        `self.per_page` as the `per_page` argument to the built-in
        Django `Paginator`. This will return `paginator.page` for the
        page number passed in.
        """
        paginator = Paginator(data, per_page=self.per_page)
        return paginator.page(page)

    @cachebusterable
    def get(self, request, *args, **kwargs):
        """
        Default 'GET' class-based view.
        """

        params = request.params
        page = 1
        if 'page' in params:
            page = int(params.pop('page'))

        sort_by = []
        if 'sort_by' in params:
            sort_by = params.pop('sort_by').split(",")

        fields = self.default_fields
        if 'fields' in params:
            fields = params.pop('fields').split(",")

        data = self.get_query_set(request, *args, **kwargs)
        data = self.filter(data, **params)
        data = self.sort(data, sort_by)

        try:
            data_page = self.paginate(data, page)
        except EmptyPage:
            raise HttpError(
                404,
                'No such page (heh, literally - its out of bounds)'
            )

        try:
            related, config = get_fields(self.serialize_config, fields=fields)
        except FieldKeyError as e:
            raise HttpError(400, "Error: You've asked for a field (%s) that "
                            "is invalid. Check the docs for this model." % (
                                e.field))
        except KeyError as e:
            raise HttpError(400, "Error: Invalid field: %s" % (e))

        data = data.prefetch_related(*related)
        # print("Related: %s" % (related))

        response = Http200({
            "meta": {
                "count": len(data_page.object_list),
                "page": page,
                "per_page": self.per_page,
                "max_page": data_page.end_index(),
                "total_count": data.count(),
            },
            "results": [
                serialize(x, **config)
                for x in data_page.object_list
            ]
        })

        response['Access-Control-Allow-Origin'] = "*"
        return response


class PublicDetailEndpoint(DetailEndpoint):
    """
    Imago public detail view API helper class.

    This class exists to be subclassed by concrete views, and builds in
    sane default behavior for all list views.

    This allows our views to be declarative, and allow for subclass overriding
    of methods when needed.

    Access-Control-Allow-Origin is currently always set to "*", since this
    is a global read-only API.

    As a result, JSONP is disabled. Read more on using CORS:
       - http://en.wikipedia.org/wiki/Cross-origin_resource_sharing


    The 'get' class-based view method uses the following object properties:

         - model            | Django ORM Model / class to query using.

         - default_fields   | If no `fields` param is passed in, use this
                            | to limit the `serialize_config`.

         - serialize_config | Object serializion to use. Many are in
                            | the imago.serialize module
    """

    methods = ['GET']

    @cachebusterable
    def get(self, request, pk, *args, **kwargs):
        params = request.params

        fields = self.default_fields
        if 'fields' in params:
            fields = params.pop('fields').split(",")

        related, config = get_fields(self.serialize_config, fields=fields)
        # print("Related: %s" % (related))
        obj = self.model.objects.prefetch_related(*related).get(pk=pk)
        response = Http200(serialize(obj, **config))
        response['Access-Control-Allow-Origin'] = "*"

        return response
