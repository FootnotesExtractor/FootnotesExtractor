"""Loaded copies ("instances") of a model, so documents can be processed side
by side: each job checks out its own copy of every model it uses for as long
as it runs (lease), and the next job waiting for that model takes it when it
is handed back. How many copies of a model may exist is the admin's setting
(Admin -> Models -> Instances, read through `size_of`).

Callers outside a job (the viewer's box OCR, re-tagging one footnote) take no
lease: they use copy 0 through current(), shared with whichever job holds
it - the engines guard what cannot run concurrently themselves (OCREngine's
per-copy lock), as they did when there was only ever one copy.

Jobs lease in one fixed order (OCR, detection, NER - see process_pdf_sync), so
two jobs never wait on each other's copies.
"""
import gc
import threading
from contextlib import contextmanager

# Every model load in the process takes this lock: loading changes process-wide
# PyTorch state - Hugging Face's from_pretrained(torch_dtype=...) sets the
# default dtype while it builds the model - so two loads at once (a float16
# Surya copy beside a float32 NER suite) build models half in one dtype, half
# in the other (every job on the second Surya copy failed with
# "expected mat1 and mat2 to have the same dtype"). The app's job workers also
# wait on it before each job, so none starts while models load.
MODEL_LOAD_LOCK = threading.RLock()


class InstancePool:
    def __init__(self, create, name=''):
        self._create = create            # key -> a new, loaded copy
        self.name = name
        self.size_of = lambda key: 1     # key -> copies allowed (set by the app)
        self._cond = threading.Condition()
        self._copies = {}                # key -> [copies], copy 0 first
        self._free = {}                  # key -> [copies not leased]
        self._loading = {}               # key -> copies being created
        self._retired = set()            # keys unloaded: leased copies go when handed back
        self._local = threading.local()

    # -- callers outside a job -------------------------------------------------

    def current(self, key):
        """The copy this thread leased for `key`, or the shared copy 0."""
        leased = getattr(self._local, 'leases', {}).get(key)
        if leased is not None:
            return leased
        with self._cond:
            while not self._copies.get(key):
                if not self._loading.get(key):    # nobody loading one: we do
                    self._loading[key] = 1
                    break
                self._cond.wait()                 # the copy being loaded will do
            else:
                return self._copies[key][0]
        return self._new(key, into_free=True)

    # -- jobs --------------------------------------------------------------------

    @contextmanager
    def lease(self, key):
        """A copy of `key` for this thread alone until the block ends; waits
        while every allowed copy is leased."""
        leases = self._local.__dict__.setdefault('leases', {})
        if key in leases:                # nested lease of the same model
            yield leases[key]
            return
        copy = None
        with self._cond:
            while True:
                free = self._free.get(key)
                if free:
                    copy = free.pop()
                    break
                if self._count(key) < max(1, self.size_of(key)):
                    self._loading[key] = self._loading.get(key, 0) + 1
                    break
                self._cond.wait()
        if copy is None:
            copy = self._new(key, into_free=False)
        leases[key] = copy
        try:
            yield copy
        finally:
            del leases[key]
            dropped = self._give_back(key, copy)
            del copy
            if dropped:
                self._release_memory()

    def warm(self, key, n=None):
        """Load copies of `key` up to n (default: the allowed number) now,
        rather than when a job first needs them."""
        n = max(1, self.size_of(key)) if n is None else n
        while True:
            with self._cond:
                if self._count(key) >= n:
                    break
                self._loading[key] = self._loading.get(key, 0) + 1
            self._new(key, into_free=True)
        self._release_memory()        # what loading left in PyTorch's cache

    def trim(self, key=None):
        """Drop free copies above the allowed number (after the setting went
        down); leased ones go when they are handed back."""
        with self._cond:
            for k in ([key] if key is not None else list(self._copies)):
                while len(self._copies.get(k, [])) > max(1, self.size_of(k)):
                    if not self._drop_spare(k):
                        break
        self._release_memory()

    def _drop_spare(self, key):
        """Drop one free copy other than copy 0; in a function of its own so
        no name in trim() keeps it alive while its memory is released."""
        spare = [c for c in self._free.get(key, []) if c is not self._copies[key][0]]
        if spare:
            self._drop(key, spare[0])
        return bool(spare)

    def unload(self, key):
        """Drop every copy of `key` (its model was switched off) and give
        its memory back: free copies now, leased ones when their job hands
        them back. A later need for it loads it again."""
        with self._cond:
            self._retired.add(key)
            while self._free.get(key):
                self._drop(key, self._free[key][0])
        self._release_memory()            # nothing here refers to the dropped copies any more

    def loaded(self):
        """{key: (copies loaded, copies leased)}"""
        with self._cond:
            return {k: (len(c), len(c) - len(self._free.get(k, []))) for k, c in self._copies.items() if c}

    # -- internals ----------------------------------------------------------------

    def _count(self, key):
        return len(self._copies.get(key, [])) + self._loading.get(key, 0)

    def _new(self, key, into_free):
        """Load a copy; the caller has counted it in _loading."""
        try:
            with MODEL_LOAD_LOCK:
                copy = self._create(key)
        except BaseException:
            with self._cond:
                self._loading[key] -= 1
                self._cond.notify_all()
            raise
        with self._cond:
            self._loading[key] -= 1
            self._retired.discard(key)           # wanted again
            self._copies.setdefault(key, []).append(copy)
            self._free.setdefault(key, [])
            if into_free:
                self._free[key].append(copy)
            self._cond.notify_all()
        return copy

    def _give_back(self, key, copy):
        with self._cond:
            copies = self._copies.get(key, [])
            if key in self._retired or (len(copies) > max(1, self.size_of(key)) and copy is not copies[0]):
                self._drop(key, copy)
                dropped = True
            else:
                self._free.setdefault(key, []).append(copy)
                dropped = False
            self._cond.notify_all()
        return dropped                    # the caller releases the memory once it lets go too

    def _drop(self, key, copy):
        self._copies[key].remove(copy)
        if copy in self._free.get(key, []):
            self._free[key].remove(copy)

    @staticmethod
    def release_memory():
        """Give PyTorch's cached GPU memory back to the device, e.g. before
        showing how much is in use."""
        InstancePool._release_memory()

    @staticmethod
    def _release_memory():
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
