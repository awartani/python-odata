# -*- coding: utf-8 -*-

import logging
import sys
has_lxml = False
try:
    from lxml import etree as ET
    has_lxml = True
except ImportError:
    if sys.version_info < (2, 7):
        raise ImportError('lxml required for Python versions older than 2.7')
    from xml.etree import ElementTree as ET

from .entity import declarative_base, EntityBase
from .property import StringProperty, IntegerProperty, DecimalProperty, \
    DatetimeProperty, BooleanProperty, NavigationProperty, UUIDProperty
from .enumtype import EnumType, EnumTypeProperty


class MetaData(object):

    log = logging.getLogger('odata.metadata')
    namespaces = {
        'edm': 'http://docs.oasis-open.org/odata/ns/edm',
        'edmx': 'http://docs.oasis-open.org/odata/ns/edmx'
    }

    property_types = {
        'Edm.Int16': IntegerProperty,
        'Edm.Int32': IntegerProperty,
        'Edm.Int64': IntegerProperty,
        'Edm.String': StringProperty,
        'Edm.Single': DecimalProperty,
        'Edm.Decimal': DecimalProperty,
        'Edm.DateTimeOffset': DatetimeProperty,
        'Edm.Boolean': BooleanProperty,
        'Edm.Guid': UUIDProperty,
    }

    def __init__(self, service, metadata_local_file_path=None):
        self.url = service.url + '$metadata/'
        self.connection = service.default_context.connection
        self.service = service
        self.metadata_local_file_path = metadata_local_file_path

    def property_type_to_python(self, edm_type):
        return self.property_types.get(edm_type, StringProperty)

    def _type_is_collection(self, typename):
        if typename.startswith('Collection('):
            stripped = typename.lstrip('Collection(').rstrip(')')
            return True, stripped
        else:
            return False, typename

    def _set_object_relationships(self, entities):
        for entity in entities.values():
            schema = entity.__odata_schema__

            for schema_nav in schema.get('navigation_properties', []):
                name = schema_nav['name']
                type_ = schema_nav['type']
                foreign_key = schema_nav['foreign_key']

                is_collection, type_ = self._type_is_collection(type_)

                for _search_entity in entities.values():
                    if _search_entity.__odata_schema__['type'] == type_:
                        nav = NavigationProperty(
                            name,
                            _search_entity,
                            collection=is_collection,
                            foreign_key=foreign_key,
                        )
                        setattr(entity, name, nav)

    def _create_entities(self, all_types, entities, entity_sets, entity_base_class, schemas):
        for schema in schemas:
            for entity_dict in schema.get('entities'):
                entity_type = entity_dict['type']
                entity_name = entity_dict['name']

                base_type = entity_dict.get('base_type')
                parent_entity_class = all_types.get(base_type)

                collection_name = entity_sets.get(entity_type, {}).get('name')

                if not collection_name:
                    collection_name = entity_name

                object_dict = dict(
                    __odata_schema__=entity_dict,
                    __odata_type__=entity_type,
                    __odata_collection__=collection_name
                )

                if base_type and parent_entity_class:
                    entity_class = type(entity_name, (parent_entity_class,), object_dict)  # type: EntityBase
                    if entity_class.__odata_collection__:
                        entities[entity_name] = entity_class
                else:
                    entity_class = type(entity_name, (entity_base_class,), object_dict)
                    if collection_name:
                        entities[entity_name] = entity_class

                all_types[entity_type] = entity_class

                for prop in entity_dict.get('properties'):
                    prop_name = prop['name']

                    if hasattr(entity_class, prop_name):
                        # do not replace existing properties (from Base)
                        continue

                    property_type = all_types.get(prop['type'])

                    if property_type and issubclass(property_type, EnumType):
                        property_instance = EnumTypeProperty(prop_name, enum_class=property_type)
                    else:
                        type_ = self.property_type_to_python(prop['type'])
                        type_options = {
                            'primary_key': prop['is_primary_key'],
                            'is_collection': prop['is_collection'],
                        }
                        property_instance = type_(prop_name, **type_options)
                    setattr(entity_class, prop_name, property_instance)

    def _create_actions(self, entities, actions, get_entity_or_prop_from_type):
        for action in actions:
            entity_type = action['is_bound_to']
            bind_entity = None
            bound_to_collection = False
            if entity_type:
                bound_to_collection, entity_type = self._type_is_collection(entity_type)
                for entity in entities.values():
                    schema = entity.__odata_schema__
                    if schema['type'] == entity_type:
                        bind_entity = entity

            parameters_dict = {}
            for param in action['parameters']:
                parameters_dict[param['name']] = self.property_type_to_python(param['type'])

            object_dict = dict(
                __odata_service__=self.service,
                name=action['fully_qualified_name'],
                parameters=parameters_dict,
                return_type=get_entity_or_prop_from_type(action['return_type']),
                return_type_collection=get_entity_or_prop_from_type(action['return_type_collection']),
                bound_to_collection=bound_to_collection,
            )
            action_class = type(action['name'], (self.service.Action,), object_dict)

            if bind_entity:
                setattr(bind_entity, action['name'], action_class())
            else:
                self.service.actions[action['name']] = action_class()

    def _create_functions(self, entities, functions, get_entity_or_prop_from_type):
        for function in functions:
            entity_type = function['is_bound_to']
            bind_entity = None
            bound_to_collection = False
            if entity_type:
                bound_to_collection, entity_type = self._type_is_collection(entity_type)
                for entity in entities.values():
                    schema = entity.__odata_schema__
                    if schema['type'] == entity_type:
                        bind_entity = entity

            parameters_dict = {}
            for param in function['parameters']:
                parameters_dict[param['name']] = self.property_type_to_python(param['type'])

            object_dict = dict(
                __odata_service__=self.service,
                name=function['fully_qualified_name'],
                parameters=parameters_dict,
                return_type=get_entity_or_prop_from_type(function['return_type']),
                return_type_collection=get_entity_or_prop_from_type(function['return_type_collection']),
                bound_to_collection=bound_to_collection,
            )
            function_class = type(function['name'], (self.service.Function,), object_dict)

            if bind_entity:
                setattr(bind_entity, function['name'], function_class())
            else:
                self.service.functions[function['name']] = function_class()

    def get_entity_sets(self, base=None):
        document = self.load_document()
        schemas, entity_sets, actions, functions = self.parse_document(document)

        entities = {}
        base_class = base or declarative_base()
        all_types = {}

        def get_entity_or_prop_from_type(typename):
            if typename is None:
                return

            type_ = all_types.get(typename)
            if type_ is not None:
                return type_

            return self.property_type_to_python(typename)

        for schema in schemas:
            for enum_type in schema['enum_types']:
                names = [(i['name'], i['value']) for i in enum_type['members']]
                created_enum = EnumType(enum_type['name'], names=names)
                all_types[enum_type['fully_qualified_name']] = created_enum

        self._create_entities(all_types, entities, entity_sets, base_class, schemas)
        self._set_object_relationships(entities)
        self._create_actions(entities, actions, get_entity_or_prop_from_type)
        self._create_functions(entities, functions, get_entity_or_prop_from_type)

        self.log.info('Loaded {0} entity sets, total {1} types'.format(len(entities), len(all_types)))
        return base_class, entities, all_types

    def load_document(self):
        if self.metadata_local_file_path:
            self.log.info('Reading metadata document: {0}'.format(self.metadata_local_file_path))
            with open(self.metadata_local_file_path, 'r') as metadata_file:
                content = metadata_file.read()
                return ET.fromstring(content)
        else:
            self.log.info('Loading metadata document: {0}'.format(self.url))
            response = self.connection._do_get(self.url)
            return ET.fromstring(response.content)

    def _parse_action(self, xmlq, action_element, schema_name):
        action = {
            'name': action_element.attrib['Name'],
            'fully_qualified_name': action_element.attrib['Name'],
            'is_bound': action_element.attrib.get('IsBound') == 'true',
            'is_bound_to': None,
            'parameters': [],
            'return_type': None,
            'return_type_collection': None,
        }

        if action['is_bound']:
            # bound actions are named SchemaNamespace.ActionName
            action['fully_qualified_name'] = '.'.join([schema_name, action['name']])

        for parameter_element in xmlq(action_element, 'edm:Parameter'):
            parameter_name = parameter_element.attrib['Name']
            if action['is_bound'] and parameter_name == 'bindingParameter':
                action['is_bound_to'] = parameter_element.attrib['Type']
                continue

            parameter_type = parameter_element.attrib['Type']

            action['parameters'].append({
                'name': parameter_name,
                'type': parameter_type,
            })

        for return_type_element in xmlq(action_element, 'edm:ReturnType'):
            type_name = return_type_element.attrib['Type']
            is_collection, type_name = self._type_is_collection(type_name)
            if is_collection:
                action['return_type_collection'] = type_name
            else:
                action['return_type'] = type_name
        return action

    def _parse_function(self, xmlq, function_element, schema_name):
        function = {
            'name': function_element.attrib['Name'],
            'fully_qualified_name': function_element.attrib['Name'],
            'is_bound': function_element.attrib.get('IsBound') == 'true',
            'is_bound_to': None,
            'parameters': [],
            'return_type': None,
            'return_type_collection': None,
        }

        if function['is_bound']:
            # bound functions are named SchemaNamespace.FunctionName
            function['fully_qualified_name'] = '.'.join(
                [schema_name, function['name']])

        for parameter_element in xmlq(function_element, 'edm:Parameter'):
            parameter_name = parameter_element.attrib['Name']
            if function['is_bound'] and parameter_name == 'bindingParameter':
                function['is_bound_to'] = parameter_element.attrib['Type']
                continue

            parameter_type = parameter_element.attrib['Type']

            function['parameters'].append({
                'name': parameter_name,
                'type': parameter_type,
            })

        for return_type_element in xmlq(function_element, 'edm:ReturnType'):
            type_name = return_type_element.attrib['Type']
            is_collection, type_name = self._type_is_collection(type_name)
            if is_collection:
                function['return_type_collection'] = type_name
            else:
                function['return_type'] = type_name
        return function

    def get_type_name(self, name, schema_name):
        # @TODO: this should be changes to support the real aliases
        return name.replace('mscrm', 'Microsoft.Dynamics.CRM')

    def _parse_entity(self, xmlq, entity_element, schema_name):
        entity_name = entity_element.attrib['Name']

        entity_type_name = '.'.join([schema_name, entity_name])

        entity = {
            'name': entity_name,
            'type': entity_type_name,
            'properties': [],
            'navigation_properties': [],
        }

        base_type = entity_element.attrib.get('BaseType')

        if base_type:
            entity['base_type'] = self.get_type_name(base_type, schema_name)

        entity_pks = {}
        for pk_property in xmlq(entity_element, 'edm:Key/edm:PropertyRef'):
            pk_property_name = pk_property.attrib['Name']
            entity_pks[pk_property_name] = 0

        for entity_property in xmlq(entity_element, 'edm:Property'):
            p_name = entity_property.attrib['Name']
            p_type = self.get_type_name(entity_property.attrib['Type'], schema_name)

            is_collection, p_type = self._type_is_collection(p_type)
            entity['properties'].append({
                'name': p_name,
                'type': p_type,
                'is_primary_key': p_name in entity_pks,
                'is_collection': is_collection,
            })

        for nav_property in xmlq(entity_element, 'edm:NavigationProperty'):
            p_name = nav_property.attrib['Name']
            p_type = self.get_type_name(nav_property.attrib['Type'], schema_name)
            p_foreign_key = None

            ref_constraint = xmlq(nav_property, 'edm:ReferentialConstraint')
            if ref_constraint:
                ref_constraint = ref_constraint[0]
                p_foreign_key = ref_constraint.attrib['Property']

            entity['navigation_properties'].append({
                'name': p_name,
                'type': p_type,
                'foreign_key': p_foreign_key,
            })
        return entity

    def _parse_enumtype(self, xmlq, enumtype_element, schema_name):
        enum_name = enumtype_element.attrib['Name']
        enum = {
            'name': enum_name,
            'fully_qualified_name': '.'.join([schema_name, enum_name]),
            'members': []
        }
        for enum_member in xmlq(enumtype_element, 'edm:Member'):
            member_name = enum_member.attrib['Name']
            member_value = int(enum_member.attrib['Value'])
            enum['members'].append({
                'name': member_name,
                'value': member_value,
            })
        return enum

    def parse_document(self, doc):
        schemas = []
        container_sets = {}
        actions = []
        functions = []

        if has_lxml:
            def xmlq(node, xpath):
                return node.xpath(xpath, namespaces=self.namespaces)
        else:
            def xmlq(node, xpath):
                return node.findall(xpath, namespaces=self.namespaces)

        for schema in xmlq(doc, 'edmx:DataServices/edm:Schema'):
            schema_name = schema.attrib['Namespace']

            schema_dict = {
                'name': schema_name,
                'entities': [],
                'enum_types': [],
                'complex_types': [],
            }

            for enum_type in xmlq(schema, 'edm:EnumType'):
                enum = self._parse_enumtype(xmlq, enum_type, schema_name)
                schema_dict['enum_types'].append(enum)

            for entity_type in xmlq(schema, 'edm:EntityType'):

                entity = self._parse_entity(xmlq, entity_type, schema_name)
                schema_dict['entities'].append(entity)

            schemas.append(schema_dict)

        for schema in xmlq(doc, 'edmx:DataServices/edm:Schema'):
            schema_name = schema.attrib['Namespace']
            for entity_set in xmlq(schema, 'edm:EntityContainer/edm:EntitySet'):
                set_name = entity_set.attrib['Name']
                set_type = entity_set.attrib['EntityType']

                set_type = self.get_type_name(set_type, schema_name)

                set_dict = {
                    'name': set_name,
                    'type': set_type,
                    'schema': None,
                }

                for schema_ in schemas:
                    for entity in schema_.get('entities', []):
                        if entity.get('type') == set_type:
                            set_dict['schema'] = entity

                container_sets[set_type] = set_dict

            for action_def in xmlq(schema, 'edm:Action'):
                action = self._parse_action(xmlq, action_def, schema_name)
                actions.append(action)

            for function_def in xmlq(schema, 'edm:Function'):
                function = self._parse_function(xmlq, function_def, schema_name)
                functions.append(function)

        return schemas, container_sets, actions, functions
