# Django clinic_backend module initialization with Python 3.14 compatibility patch

import django.template.context

def _patched_base_context_copy(self):
    duplicate = self.__class__.__new__(self.__class__)
    duplicate.__dict__.update(self.__dict__)
    duplicate.dicts = self.dicts[:]
    return duplicate

def _patched_context_copy(self):
    return _patched_base_context_copy(self)

def _patched_request_context_copy(self):
    return _patched_base_context_copy(self)

django.template.context.BaseContext.__copy__ = _patched_base_context_copy
django.template.context.Context.__copy__ = _patched_context_copy
django.template.context.RequestContext.__copy__ = _patched_request_context_copy
