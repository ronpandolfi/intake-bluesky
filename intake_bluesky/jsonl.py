import event_model
from functools import partial
import intake
import intake.catalog
import intake.catalog.local
import intake.source.base
import json
from mongoquery import Query

from .core import parse_handler_registry


class BlueskyJSONLCatalog(intake.catalog.Catalog):
    def __init__(self, jsonl_filelist, *,
                 handler_registry=None, query=None, **kwargs):
        """
        This Catalog is backed by a newline-delimited JSON (jsonl) file.

        Each line of the file is expected to be a JSON list with two elements,
        the document name (type) and the document itself. The documents are
        expected to be in chronological order.

        Parameters
        ----------
        jsonl_filelist : list
            list of filepaths
        handler_registry : dict, optional
            Maps each asset spec to a handler class or a string specifying the
            module name and class name, as in (for example)
            ``{'SOME_SPEC': 'module.submodule.class_name'}``.
        **kwargs :
            Additional keyword arguments are passed through to the base class,
            Catalog.
        """
        name = 'bluesky-jsonl-catalog'  # noqa
        self._runs = {}  # This maps run_start_uids to file paths.
        self._run_starts = {}  # This maps run_start_uids to run_start_docs.

        self._query = query or {}
        if handler_registry is None:
            handler_registry = {}
        parsed_handler_registry = parse_handler_registry(handler_registry)
        self.filler = event_model.Filler(parsed_handler_registry)
        self._update_index(jsonl_filelist)
        super().__init__(**kwargs)

    def _update_index(self, file_list):
        for run_file in file_list:
            with open(run_file, 'r') as f:
                name, run_start_doc = json.loads(f.readline())

                if name != 'start':
                    raise ValueError(
                        f"Invalid file {run_file}: "
                        f"first line must be a valid start document.")

                if Query(self._query).match(run_start_doc):
                    run_start_uid = run_start_doc['uid']
                    self._runs[run_start_uid] = run_file
                    self._run_starts[run_start_uid] = run_start_doc

    def _get_run_stop(self, run_start_uid):
        with open(self._runs[run_start_uid], 'r') as run_file:
            name, doc = json.loads(run_file.readlines()[-1])
        if name == 'stop':
            return doc
        else:
            return None

    def _get_event_descriptors(self, run_start_uid):
        descriptors = []
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'descriptor':
                    descriptors.append(doc)
        return descriptors

    def _get_event_cursor(self, run_start_uid, descriptor_uids, skip=0, limit=None):
        skip_counter = 0
        descriptor_set = set(descriptor_uids)
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'event' and doc['descriptor'] in descriptor_set:
                    if skip_counter >= skip and (limit is None or skip_counter < limit):
                        yield doc
                    skip_counter += 1
                    if limit is not None and skip_counter >= limit:
                        break

    def _get_event_count(self, run_start_uid, descriptor_uids):
        event_count = 0
        descriptor_set = set(descriptor_uids)
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'event' and doc['descriptor'] in descriptor_set:
                    event_count += 1
        return event_count

    def _get_resource(self, run_start_uid, uid):
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'resource' and doc['uid'] == uid:
                    return doc
        raise ValueError(f"Resource uid {uid} not found.")

    def _get_datum(self, run_start_uid, datum_id):
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'datum' and doc['datum_id'] == datum_id:
                    return doc
        raise ValueError(f"Datum_id {datum_id} not found.")

    def _get_datum_cursor(self, run_start_uid, resource_uid, skip=0, limit=None):
        skip_counter = 0
        with open(self._runs[run_start_uid], 'r') as run_file:
            for line in run_file:
                name, doc = json.loads(line)
                if name == 'datum' and doc['resource'] == resource_uid:
                    if skip_counter >= skip and (limit is None or skip_counter < limit):
                        yield doc
                    skip_counter += 1
                    if limit is not None and skip_counter >= limit:
                        return

    def _make_entries_container(self):
        catalog = self

        class Entries:
            "Mock the dict interface around a MongoDB query result."
            def _doc_to_entry(self, run_start_doc):
                uid = run_start_doc['uid']
                entry_metadata = {'start': run_start_doc,
                                  'stop': catalog._get_run_stop(uid)}
                args = dict(
                    get_run_start=lambda: run_start_doc,
                    get_run_stop=partial(catalog._get_run_stop, uid),
                    get_event_descriptors=partial(catalog._get_event_descriptors, uid),
                    get_event_cursor=partial(catalog._get_event_cursor, uid),
                    get_event_count=partial(catalog._get_event_count, uid),
                    get_resource=partial(catalog._get_resource, uid),
                    get_datum=partial(catalog._get_datum, uid),
                    get_datum_cursor=partial(catalog._get_datum_cursor, uid),
                    filler=catalog.filler)
                return intake.catalog.local.LocalCatalogEntry(
                    name=run_start_doc['uid'],
                    description={},  # TODO
                    driver='intake_bluesky.core.RunCatalog',
                    direct_access='forbid',  # ???
                    args=args,
                    cache=None,  # ???
                    parameters=[],
                    metadata=entry_metadata,
                    catalog_dir=None,
                    getenv=True,
                    getshell=True,
                    catalog=catalog)

            def __iter__(self):
                yield from self.keys()

            def keys(self):
                yield from catalog._runs

            def values(self):
                for run_start_doc in catalog._run_starts.values():
                    yield self._doc_to_entry(run_start_doc)

            def items(self):
                for uid, run_start_doc in catalog._run_starts.items():
                    yield uid, self._doc_to_entry(run_start_doc)

            def __getitem__(self, name):
                # If this came from a client, we might be getting '-1'.
                try:
                    name = int(name)
                except ValueError:
                    pass
                if isinstance(name, int):
                    raise NotImplementedError
                else:
                    run_start_doc = catalog._run_starts[name]
                    return self._doc_to_entry(run_start_doc)

            def __contains__(self, key):
                # Avoid iterating through all entries.
                try:
                    self[key]
                except KeyError:
                    return False
                else:
                    return True

        return Entries()

    def search(self, query):
        """
        Return a new Catalog with a subset of the entries in this Catalog.

        Parameters
        ----------
        query : dict
        """
        if self._query:
            query = {'$and': [self._query, query]}
        cat = type(self)(
            jsonl_filelist=list(self._runs.values()),
            query=query,
            name='search results',
            getenv=self.getenv,
            getshell=self.getshell,
            auth=self.auth,
            metadata=(self.metadata or {}).copy(),
            storage_options=self.storage_options)
        return cat
