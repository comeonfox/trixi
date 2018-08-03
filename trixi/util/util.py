import ast
import importlib
import json
import logging
import numpy as np
import os
import random
import string
import matplotlib.pyplot as plt
import time
import traceback
import warnings
from collections import defaultdict
from hashlib import sha256
from tempfile import gettempdir
from types import FunctionType, ModuleType

import portalocker
try:
    import torch
except ImportError as e:
    print("Could not import Pytorch related modules.")
    print(e)

    class torch: dtype = None


class CustomJSONEncoder(json.JSONEncoder):

    def _encode(self, obj):
        raise NotImplementedError

    def _encode_switch(self, obj):
        if isinstance(obj, list):
            return [self._encode_switch(item) for item in obj]
        elif isinstance(obj, dict):
            return {self._encode_key(key): self._encode_switch(val) for key, val in obj.items()}
        else:
            return self._encode(obj)

    def _encode_key(self, obj):
        return self._encode(obj)

    def encode(self, obj):
        return super(CustomJSONEncoder, self).encode(self._encode_switch(obj))

    def iterencode(self, obj, *args, **kwargs):
        return super(CustomJSONEncoder, self).iterencode(self._encode_switch(obj), *args, **kwargs)


class MultiTypeEncoder(CustomJSONEncoder):

    def _encode_key(self, obj):
        if isinstance(obj, int):
            return "__int__({})".format(obj)
        elif isinstance(obj, float):
            return "__float__({})".format(obj)
        else:
            return self._encode(obj)

    def _encode(self, obj):
        if isinstance(obj, tuple):
            return "__tuple__({})".format(obj)
        elif isinstance(obj, np.integer):
            return "__int__({})".format(obj)
        elif isinstance(obj, np.floating):
            return "__float__({})".format(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return obj


class ModuleMultiTypeEncoder(MultiTypeEncoder):

    def _encode(self, obj, strict=False):
        if type(obj) == type:
            return "__type__({}.{})".format(obj.__module__, obj.__name__)
        elif type(obj) == torch.dtype:
            return "__type__({})".format(str(obj))
        elif isinstance(obj, FunctionType):
            return "__function__({}.{})".format(obj.__module__, obj.__name__)
        elif isinstance(obj, ModuleType):
            return "__module__({})".format(obj.__name__)
        else:
            try:
                return super(ModuleMultiTypeEncoder, self)._encode(obj)
            except Exception as e:
                if strict:
                    raise e
                else:
                    message = "Could not pickle object of type {}\n".format(type(obj))
                    message += traceback.format_exc()
                    warnings.warn(message)
                    return repr(obj)


class CustomJSONDecoder(json.JSONDecoder):

    def _decode(self, obj):
        raise NotImplementedError

    def _decode_switch(self, obj):
        if isinstance(obj, list):
            return [self._decode_switch(item) for item in obj]
        elif isinstance(obj, dict):
            return {self._decode_key(key): self._decode_switch(val) for key, val in obj.items()}
        else:
            return self._decode(obj)

    def _decode_key(self, obj):
        return self._decode(obj)

    def decode(self, obj):
        return self._decode_switch(super(CustomJSONDecoder, self).decode(obj))


class MultiTypeDecoder(CustomJSONDecoder):

    def _decode(self, obj):
        if isinstance(obj, str):
            if obj.startswith("__int__"):
                return int(obj[8:-1])
            elif obj.startswith("__float__"):
                return float(obj[10:-1])
            elif obj.startswith("__tuple__"):
                return tuple(ast.literal_eval(obj[10:-1]))
        return obj


class ModuleMultiTypeDecoder(MultiTypeDecoder):

    def _decode(self, obj):
        if isinstance(obj, str):
            if obj.startswith("__type__"):
                str_ = obj[9:-1]
                module_ = ".".join(str_.split(".")[:-1])
                name_ = str_.split(".")[-1]
                return getattr(importlib.import_module(module_), name_)
            elif obj.startswith("__function__"):
                str_ = obj[13:-1]
                module_ = ".".join(str_.split(".")[:-1])
                name_ = str_.split(".")[-1]
                return getattr(importlib.import_module(module_), name_)
            elif obj.startswith("__module__"):
                return importlib.import_module(obj[11:-1])
        return super(ModuleMultiTypeDecoder, self)._decode(obj)


class Singleton:
    """
    A non-thread-safe helper class to ease implementing singletons.
    This should be used as a decorator -- not a metaclass -- to the
    class that should be a singleton.

    The decorated class can define one `__init__` function that
    takes only the `self` argument. Also, the decorated class cannot be
    inherited from. Other than that, there are no restrictions that apply
    to the decorated class.

    To get the singleton instance, use the `Instance` method. Trying
    to use `__call__` will result in a `TypeError` being raised.

    """

    _instance = None

    def __init__(self, decorated):
        self._decorated = decorated

    def get_instance(self, **kwargs):
        """
        Returns the singleton instance. Upon its first call, it creates a
        new instance of the decorated class and calls its `__init__` method.
        On all subsequent calls, the already created instance is returned.

        """
        if not self._instance:
            self._instance = self._decorated(**kwargs)
            return self._instance
        else:
            return self._instance

    def __call__(self):
        raise TypeError('Singletons must be accessed through `get_instance()`.')
        # return self.get_instance()

    def __instancecheck__(self, inst):
        return isinstance(inst, self._decorated)


def savefig_and_close(figure, *args, **kwargs):
    figure.savefig(*args, **kwargs)
    plt.close(figure)


def random_string(length):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(length))


def create_folder(path):
    """
    Creates a folder if not already exists
    Args:
        :param path: The folder to be created
    Returns
        :return: True if folder was newly created, false if folder already exists
    """

    if not os.path.exists(path):
        os.makedirs(path)
        return True
    else:
        return False


def name_and_iter_to_filename(name, n_iter, ending, iter_format="{:05d}", prefix=False):
    iter_str = iter_format.format(n_iter)
    if prefix:
        name = iter_str + "_" + name + ending
    else:
        name = name + "_" + iter_str + ending

    return name


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


class PyLock(object):
    def __init__(self, name, timeout, check_interval=0.25):
        self._timeout = timeout
        self._check_interval = check_interval

        lock_directory = gettempdir()
        unique_token = sha256(name.encode()).hexdigest()
        self._filepath = os.path.join(lock_directory, 'ilock-' + unique_token + '.lock')

    def __enter__(self):

        current_time = call_time = time.time()
        while call_time + self._timeout > current_time:
            self._lockfile = open(self._filepath, 'w')
            try:
                portalocker.lock(self._lockfile, portalocker.constants.LOCK_NB | portalocker.constants.LOCK_EX)
                return self
            except portalocker.exceptions.LockException:
                pass

            current_time = time.time()
            check_interval = self._check_interval if self._timeout > self._check_interval else self._timeout
            time.sleep(check_interval)

        raise RuntimeError('Timeout was reached')

    def __exit__(self, exc_type, exc_val, exc_tb):
        portalocker.unlock(self._lockfile)
        self._lockfile.close()


class LogDict(dict):
    def __init__(self, file_name, base_dir=None, to_console=False):
        """Initializes a new Dict which can log to a given target file."""

        super(LogDict, self).__init__()

        self.file_name = file_name
        if base_dir is not None:
            self.file_name = os.path.join(base_dir, file_name)

        self.logging_identifier = random_string(15)
        self.logger = logging.getLogger("logdict-" + self.logging_identifier)
        self.logger.setLevel(logging.INFO)
        file_handler_formatter = logging.Formatter('')

        file_handler = logging.FileHandler(self.file_name)
        file_handler.setFormatter(file_handler_formatter)
        self.logger.addHandler(file_handler)
        self.logger.propagate = to_console

    def __setitem__(self, key, item):
        super(LogDict, self).__setitem__(key, item)

    def log_complete_content(self):
        """Logs the current content of the dict to the output file as a whole."""
        self.logger.info(str(self))


class ResultLogDict(LogDict):
    def __init__(self, file_name, base_dir=None):
        """Initializes a new Dict which directly logs value changes to a given target_file."""
        super(ResultLogDict, self).__init__(file_name=file_name, base_dir=base_dir)

        self.is_init = False
        self.cntr_dict = defaultdict(float)
        self.is_init = True

    def __setitem__(self, key, item):

        if key == "cntr_dict":
            raise ValueError("In ResultLogDict you can not add an item with key 'cntr_dict'")

        data = item
        if isinstance(item, dict) and "data" in item and "label" in item and "epoch" in item:
            data = item["data"]
            if "counter" in item and item["counter"] is not None:
                self.cntr_dict[key] = item["counter"]
            json_dict = {key: ResultElement(data=data, label=item["label"], epoch=item["epoch"],
                                            counter=self.cntr_dict[key])}
        else:
            json_dict = {key: ResultElement(data=data, counter=self.cntr_dict[key])}
        self.cntr_dict[key] += 1
        self.logger.info(json.dumps(json_dict) + ",")

        super(ResultLogDict, self).__setitem__(key, data)

    def print_to_file(self, text):
        self.logger.info(text)

    def load(self, reload_dict):
        for key, item in reload_dict.items():
            print(key)
            if isinstance(item, dict) and "data" in item and "label" in item and "epoch" in item:
                data = item["data"]
                if "counter" in item and item["counter"] is not None:
                    self.cntr_dict[key] = item["counter"]
            else:
                data = item
            self.cntr_dict[key] += 1

        super(ResultLogDict, self).__setitem__(key, data)


class ResultElement(dict):
    def __init__(self, data=None, label=None, epoch=None, counter=None):
        super(ResultElement, self).__init__()

        if data is not None:
            if issubclass(type(data), np.float):
                data = float(data)
            if issubclass(type(data), np.int):
                data = int(data)
            self["data"] = data
        if label is not None:
            self["label"] = label
        if epoch is not None:
            self["epoch"] = epoch
        if counter is not None:
            self["counter"] = counter
