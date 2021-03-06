import re, collections, json
from memoized_property import memoized_property
import pandas as pd
import numpy as np

import jardin.model
import jardin.config as config


class PGQueryBuilder(object):

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    @memoized_property
    def model_metadata(self):
        return self.kwargs['model_metadata']

    @memoized_property
    def table_name(self):
        return self.model_metadata['table_name']

    @memoized_property
    def table_alias(self):
        return self.model_metadata['table_alias']

    @memoized_property
    def belongs_to(self):
        return self.model_metadata['belongs_to']

    @memoized_property
    def scopes(self):
        return self.model_metadata['scopes']

    def extrapolators(self, fields, sep = ', '):
        extrapolators = []
        for field in fields: extrapolators.append('%(' + '%s' % field + ')s')
        return sep.join(extrapolators)

    @memoized_property
    def stack(self):
        return self.kwargs.get('stack', '')

    @memoized_property
    def watermark(self):
        return "/*%s | %s */ " % (config.WATERMARK, self.stack)


class SelectQueryBuilder(PGQueryBuilder):

    where_values = {}

    @memoized_property
    def selects(self):
        selects = self.kwargs.get('select') or '*'
        if isinstance(selects, str):
            return selects
        elif isinstance(selects, list):
            return ', '.join(selects)
        elif isinstance(selects, dict):
            result = []
            for (k, v) in selects.items():
                result += ['%s AS %s' % (v, k)]
            return ', '.join(result)

    @memoized_property
    def froms(self):
        if self.table_alias is not None:
            return "%(table_name)s %(table_alias)s" % {'table_name': self.table_name, 'table_alias': self.table_alias}
        else:
            return self.table_name

    @memoized_property
    def scope_wheres(self):
        scopes = self.kwargs.get('scopes', [])
        if not isinstance(scopes, list): scopes = [scopes]
        results = []
        for scope in scopes:
            scp = self.scopes[scope]
            if not isinstance(scp, list): scp = [scp]
            results += scp
        return results

    @memoized_property
    def wheres(self):
        self.where_values = collections.OrderedDict()
        wheres = self.kwargs.get('where', None)
        if not isinstance(wheres, list): wheres = [wheres]
        wheres += self.scope_wheres
        res = [self.where_items(where) for where in wheres]
        results = ['(%s)' % ' '.join(item) for sublist in res for item in sublist]
        return ' AND '.join(results)

    def where_key(self, key):
        return 'val_%s' % len(self.where_values)

    def add_to_where_values(self, key, value):
        if isinstance(value, pd.Series) or isinstance(value, list):
            value = tuple(value)
        key = self.where_key(key)
        self.where_values[key] = value
        return '%(' + key + ')s'

    def where_items(self, where):
        results = []
        if isinstance(where, str):
            results += [[where]]
        elif isinstance(where, tuple):
            results += [[self.add_to_where_values(*where)]]
        elif isinstance(where, dict):
            for (k, v) in where.items():
                if isinstance(v, tuple):
                    from_label = self.add_to_where_values(k, v[0])
                    to_label = self.add_to_where_values(k, v[1])
                    results += [[k, 'BETWEEN', from_label, 'AND', to_label]]
                elif isinstance(v, dict):
                    for (kk, vv) in v.items():
                        res = "(" + k + "->>'" + kk + "')"
                        if isinstance(vv, int):
                            res += '::INTEGER'
                        elif isinstance(vv, float):
                            res += '::FLOAT'
                        results += [[res, '=', self.add_to_where_values(kk, vv)]]
                elif not isinstance(v, list) and not isinstance(v, pd.Series) and not isinstance(v, np.ndarray) and pd.isnull(v):
                    results += [[k, 'IS NULL']]
                elif callable(v):
                    results += [[k, v()]]
                else:
                    if isinstance(v, list) or isinstance(v, pd.Series):
                        operator = 'IN'
                    else:
                        operator = '='
                    results += [[k, operator, self.add_to_where_values(k, v)]]
        elif isinstance(where, list):
            result = where[0]
            for l in re.findall('%\((\S+)\)s', result):
                result = re.sub('%\(' + l + '\)s', '%(' + l + ')s', result)
            for (k, v) in where[1].items():
                key = self.add_to_where_values(k, v)
                result = re.sub('%\(' + k + '\)s', key, result)
            results += [[result]]
        return results

    @memoized_property
    def limit(self):
        return self.kwargs.get('limit', None)

    @memoized_property
    def having(self):
        return self.kwargs.get('having', None)

    @memoized_property
    def order_bys(self):
        return self.kwargs.get('order', None)

    @memoized_property
    def group_bys(self):
        return self.kwargs.get('group', None)

    @memoized_property
    def left_joins(self):
        joins = self.kwargs.get('left_join', None)
        return self.joins(joins, 'LEFT')

    @memoized_property
    def inner_joins(self):
        joins = self.kwargs.get('inner_join', None)
        if joins is None: return
        if isinstance(joins, str):
            return "INNER JOIN %s" % joins
        elif isinstance(joins, list):
            js = []
            for j in joins:
                if isinstance(j, str):
                    js += ["INNER JOIN %s" % j]
                elif issubclass(j, jardin.model.Model):
                    js += [self.build_join(j, how = 'INNER')]
        return ' '.join(js)

    def joins(self, joins, how):
        if joins is None: return
        if isinstance(joins, str):
            return "%s JOIN %s" % (how, joins)
        elif isinstance(joins, list):
            js = []
            for j in joins:
                if isinstance(j, str):
                    js += ["%s JOIN %s" % (how, j)]
                elif issubclass(j, jardin.model.Model):
                    js += [self.build_join(j, how = how)]
            return ' '.join(js)

    def build_join(self, join_model, how = 'INNER'):
        join_model = join_model.model_metadata()
        table_name, join_table_name = self.table_name, join_model['table_name']
        table_alias, join_table_alias = self.table_alias, join_model['table_alias']
        if self.table_name in join_model['belongs_to']:
            foreign_key = join_model['belongs_to'][table_name]
            primary_key = 'id'
        else:
            primary_key = self.model_metadata['belongs_to'][join_table_name]
            foreign_key = 'id'
        return "%(how)s JOIN %(join_table_name)s %(join_table_alias)s ON %(table_alias)s.%(primary_key)s = %(join_table_alias)s.%(foreign_key)s" % {'how': how, 'join_table_name': join_table_name, 'join_table_alias': join_table_alias, 'table_alias': table_alias, 'foreign_key': foreign_key, 'primary_key': primary_key}

    @memoized_property
    def query(self):
        query = self.watermark + "SELECT " + self.selects + ' FROM ' + self.froms
        if self.left_joins: query += ' ' + self.left_joins
        if self.inner_joins: query += ' ' + self.inner_joins
        if self.wheres: query += ' WHERE ' + self.wheres
        if self.group_bys: query += ' GROUP BY ' + self.group_bys
        if self.having: query += ' HAVING ' + self.having
        if self.order_bys: query += ' ORDER BY ' + self.order_bys
        if self.limit: query += ' LIMIT ' + str(self.limit)
        query += ';'
        return (query, self.where_values)


class WriteQueryBuilder(PGQueryBuilder):

    @memoized_property
    def write_values(self):
        kw_values = self.kwargs['values']

        if isinstance(kw_values, jardin.model.Model):
            kw_values = kw_values.attributes
        if isinstance(kw_values, dict):
            kw_values = [kw_values]
        
        kw_values = pd.DataFrame(kw_values).copy()
        kw_values.reset_index(drop=True, inplace=True)

        for col in [self.primary_key, 'stack']:
            if col in kw_values:
                del kw_values[col]

        return kw_values

    @memoized_property
    def primary_key(self):
        return self.kwargs.get('primary_key', jardin.model.Model.primary_key)

    @memoized_property
    def values_list(self):
        all_values = []

        for idx, val in self.write_values.iterrows():
            values = collections.OrderedDict()

            for k, v in val.iteritems():
                if isinstance(v, dict) or isinstance(v, list):
                    v = json.dumps(v)
                if isinstance(v, np.bool_):
                    v = bool(v)
                if isinstance(v, np.datetime64) and np.isnat(v):
                    v = None
                if isinstance(v, pd._libs.tslib.NaTType):
                    v = None
                if isinstance(v, float) and np.isnan(v):
                    v = None

                values['%s_%s' % (k, idx)] = v

            all_values += [values]
        return all_values

    @memoized_property
    def value_extrapolators(self): 
        return ', '.join([
            "(" + self.extrapolators(fa, sep = ', ') + ")"
            for fa in [v.keys() for v in self.values_list]
            ])

    @memoized_property
    def values(self):
        values = {}
        for v in self.values_list:
            values.update(**v)
        return values

    @memoized_property
    def fields(self):
        return ', '.join(self.write_values.columns)


class InsertQueryBuilder(WriteQueryBuilder):

    @memoized_property
    def query(self):
        query = self.watermark + "INSERT INTO " + self.table_name + " (" + self.fields + ") VALUES " + self.value_extrapolators + " RETURNING " + self.primary_key + ";"
        return (query, self.values)


class UpdateQueryBuilder(WriteQueryBuilder, SelectQueryBuilder):

    @memoized_property
    def query(self):
        query = self.watermark + 'UPDATE ' + self.table_name + ' SET (' + self.fields + ') = ' + self.value_extrapolators
        if self.wheres: query += " WHERE " + self.wheres
        query += ' RETURNING ' + self.primary_key + ';'
        values = self.where_values
        values.update(self.values)
        return (query, values)


class DeleteQueryBuilder(WriteQueryBuilder, SelectQueryBuilder):

    @memoized_property
    def query(self):
        query = self.watermark + 'DELETE FROM ' +self.table_name + ' WHERE ' + self.wheres + ';'
        return (query, self.where_values)


class RawQueryBuilder(WriteQueryBuilder, SelectQueryBuilder):

    def where_key(self, key):
        return key

    @memoized_property
    def sql(self):
        if 'sql' in self.kwargs and self.kwargs['sql']:
            raw_sql = self.kwargs['sql']
        if 'filename' in self.kwargs and self.kwargs['filename']:
            with open(self.kwargs['filename']) as file:
                raw_sql = file.read()
        return re.sub(r'\{(\w+?)\}', r'%(\1)s', raw_sql)

    @memoized_property
    def query(self):
        query = self.watermark + self.sql
        self.wheres
        return (query, self.where_values)
