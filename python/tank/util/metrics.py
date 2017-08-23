# Copyright (c) 2016 Shotgun Software Inc.
#
# CONFIDENTIAL AND PROPRIETARY
#
# This work is provided "AS IS" and subject to the Shotgun Pipeline Toolkit
# Source Code License included in this distribution package. See LICENSE.
# By accessing, using, copying or modifying this work you indicate your
# agreement to the Shotgun Pipeline Toolkit Source Code License. All rights
# not expressly granted therein are reserved by Shotgun Software Inc.

"""Classes and functions for logging Toolkit metrics.

Internal Use Only - We provide no guarantees that the classes and functions
here will be backwards compatible. These objects are also subject to change and
are not part of the public Sgtk API.

"""


###############################################################################
# imports

from collections import deque
from threading import Event, Thread, Lock
import urllib2
 
from . import constants

# use api json to cover py 2.5
from tank_vendor import shotgun_api3
json = shotgun_api3.shotgun.json


###############################################################################
# Metrics Queue, Dispatcher, and worker thread classes

class MetricsQueueSingleton(object):
    """A FIFO queue for logging metrics.

    This is a singleton class, so any instantiation will return the same object
    instance within the current process.

    """

    # keeps track of the single instance of the class
    __instance = None

    # A set of log identifier strings used to check whether a metric has been
    # logged already.
    __logged_metrics = set()

    def __new__(cls, *args, **kwargs):
        """Ensures only one instance of the metrics queue exists."""

        # create the queue instance if it hasn't been created already
        if not cls.__instance:

            # remember the instance so that no more are created
            metrics_queue = super(MetricsQueueSingleton, cls).__new__(
                cls, *args, **kwargs)

            metrics_queue._lock = Lock()

            # The underlying collections.deque instance
            metrics_queue._queue = deque()

            cls.__instance = metrics_queue

        return cls.__instance

    def log(self, metric, log_once=False):
        """Add the metric to the queue for dispatching.

        If ``log_once`` is set to ``True``, this will only log the metric if it
        is the first attempt to log it.

        :param EventMetric metric: The metric to log.
        :param bool log_once: ``True`` if this metric should be ignored if it
            has already been logged. ``False`` otherwise. Defaults to ``False``.
        """

        # This assumes that supplied object's classes implement __repr__
        # to return consistent results when building objects with the same
        # internal data. See the UserActivityMetric and UserAttributeMetric
        # classes below.
        metric_identifier = repr(metric)

        if log_once and metric_identifier in self.__logged_metrics:
            # the metric is already logged! nothing to do.
            return

        self._lock.acquire()
        try:
            self._queue.append(metric)

            # remember that we've logged this one already
            self.__logged_metrics.add(metric_identifier)
        except:
            pass
        finally:
            self._lock.release()

    def get_metrics(self, count=None):
        """Return `count` metrics.

        :param int count: The number of pending metrics to return.

        If `count` is not supplied, or greater than the number of pending
        metrics, returns all metrics.

        Should never raise an exception.

        """

        metrics = []

        self._lock.acquire()
        try:
            num_pending = len(self._queue)

            # there are pending metrics
            if num_pending:

                # determine how many metrics to retrieve
                if not count or count > num_pending:
                    count = num_pending

                # would be nice to be able to pop N from deque. oh well.
                metrics = [self._queue.popleft() for i in range(0, count)]
        except:
            pass
        finally:
            self._lock.release()

        return metrics


class MetricsDispatcher(object):
    """This class manages 1 or more worker threads dispatching toolkit metrics.

    After initializing the object, the `start()` method is called to
    spin up worker threads for dispatching logged metrics. The `stop()` method
    is later called to stop the worker threads.

    """

    def __init__(self, engine, num_workers=1):
        """Initialize the dispatcher object.

        :param engine: An engine instance for logging, and api access
        :param workers: The number of worker threads to start.

        """

        self._engine = engine
        self._num_workers = num_workers
        self._workers = []
        self._dispatching = False

    def start(self):
        """Starts up the workers for dispatching logged metrics.

        If called on an already dispatching instance, then result is a no-op.

        """

        if self._dispatching:
            self._engine.log_debug(
                "Metrics dispatching already started. Doing nothing.")
            return

        # Now check that we have a valid authenticated user, which is
        # required for metrics dispatch. This is to ensure certain legacy
        # and edge case scenarios work, for example the 
        # shotgun_cache_actions tank command which runs un-authenticated.
        from ..api import get_authenticated_user
        if not get_authenticated_user():
            return

        # start the dispatch workers to use this queue
        for i in range(self._num_workers):
            worker = MetricsDispatchWorkerThread(self._engine)
            worker.start()
            self._engine.log_debug("Added worker thread: %s" % (worker,))
            self._workers.append(worker)

        self._dispatching = True

    def stop(self):
        """Instructs all worker threads to stop processing metrics."""
        for worker in self.workers:
            worker.halt()

        self._dispatching = False
        self._workers = []

    @property
    def dispatching(self):
        """True if started and dispatching metrics."""
        return self._dispatching

    @property
    def workers(self):
        """A list of workers threads dispatching metrics from the queue."""
        return self._workers

class MetricsDispatchWorkerThread(Thread):
    """Worker thread for dispatching metrics to sg logging endpoint.

    Once started this worker will dispatch logged metrics to the shotgun api
    endpoint. The worker retrieves any pending metrics after the
    `DISPATCH_INTERVAL` and sends them all in a single request to sg.

    In the case metrics dispatch isn't supported by the shotgun server,
    the worker thread will exit early.
    """

    API_ENDPOINT = "api3/track_metrics/"
    """ Updated august-2017, SG API endpoint for logging metrics."""

    DISPATCH_INTERVAL = 5
    """Worker will wait this long between metrics dispatch attempts."""

    DISPATCH_BATCH_SIZE = 10
    """
    Worker will dispatch this many metrics at a time, or all if <= 0.
    NOTE: that current SG server code reject batches larger than 10.
    """

    def __init__(self, engine):
        """
        Initialize the worker thread.

        :params engine: Engine instance
        """

        super(MetricsDispatchWorkerThread, self).__init__()

        self._engine = engine

        # Make this thread a daemon. This means the process won't wait for this
        # thread to complete before exiting. In most cases, proper engine
        # shutdown should halt the worker correctly. In cases where an engine
        # is improperly shut down, this will prevent the process from hanging.
        self.daemon = True

        # makes possible to halt the thread
        self._halt_event = Event()

    def run(self):
        """Runs a loop to dispatch metrics that have been logged."""

        # first of all, check if metrics dispatch is supported
        # connect to shotgun and probe for server version
        sg_connection = self._engine.shotgun
        metrics_ok = (
            hasattr(sg_connection, "server_caps") and
            sg_connection.server_caps.version and
            sg_connection.server_caps.version >= (7, 4, 0)
        )
        if not metrics_ok:
            # metrics not supported
            return

        # run until halted
        while not self._halt_event.isSet():

            # get the next available metric and dispatch it
            try:
                metrics = MetricsQueueSingleton().get_metrics(
                    self.DISPATCH_BATCH_SIZE)
                if metrics:
                    self._dispatch(metrics)
            except Exception, e:
                pass
            finally:
                # wait, checking for halt event before more processing
                self._halt_event.wait(self.DISPATCH_INTERVAL)

    def halt(self):
        """Indiate that the worker thread should halt as soon as possible."""
        self._halt_event.set()

    def _dispatch(self, metrics):
        """Dispatch the supplied metric to the sg api registration endpoint.

        :param Metric metrics: The Toolkit metric to dispatch.

        """

        # get this thread's sg connection via tk api
        sg_connection = self._engine.tank.shotgun

        # handle proxy setup by pulling the proxy details from the main
        # shotgun connection
        if sg_connection.config.proxy_handler:
            opener = urllib2.build_opener(sg_connection.config.proxy_handler)
            urllib2.install_opener(opener)

        # build the full endpoint url with the shotgun site url
        url = "%s/%s" % (sg_connection.base_url, self.API_ENDPOINT)

        # construct the payload with the auth args and metrics data
        payload = {
            "auth_args": {
                "session_token": sg_connection.get_session_token()
            },
            "metrics": [m.data for m in metrics]
        }
        payload_json = json.dumps(payload)

        header = {'Content-Type': 'application/json'}
        try:
            request = urllib2.Request(url, payload_json, header)
            response = urllib2.urlopen(request)
        except urllib2.HTTPError, e:
            # fire and forget, so if there's an error, ignore it.
            pass

        # execute the log_metrics core hook
        self._engine.tank.execute_core_hook(
            constants.TANK_LOG_METRICS_HOOK_NAME,
            metrics=[m.data for m in metrics]
        )


###############################################################################
# ToolkitMetric classes and subclasses

class ToolkitMetric(object):
    """Simple class representing tk metric data."""

    def __init__(self, data):
        """Initialize the object with a dictionary of metric data.
        
        :param dict data: A dictionary of metric data.
        
        """
        self._data = data

    def __str__(self):
        """Readable str representation of the metric."""
        return "%s: %s" % (self.__class__, self._data)

    @property
    def data(self):
        """The underlying data this metric represents."""
        return self._data


class EventMetric(ToolkitMetric):
    """Convenience class for creating a metric event."""

    def __init__(self, event_group, event_name, event_properties=None):
        """
        Initialize a metric event using the specified parameters.

        :param str event_group: The group or category this metric falls into (see `tank.util.log_user_activity_metric()`.


            of the module in which action was performed.
        :param str action: The action that was performed.
        
        """

        #if not isinstance(event_group, str):
        #    raise TypeError("The `event_group` parameter must be None or a str")

        #if not isinstance(event_name, str):
        #    raise TypeError("The `event_name` parameter must be None or a str")

        # TODO: go for silent cast to 'str' or raise TypeError ?
        super(EventMetric, self).__init__({
            "event_group": str(event_group),
            "event_name": str(event_name)
        })

        # Initializing the event property dictionnary with a default event_type
        # somehow duplicating what's being done in Shogun (The server)
        self._data["event_property"] = {"event_type" : "event"}

        if event_properties:
            if not type(event_properties) is dict:
                raise TypeError("The `event_properties` parameter must be None or a Dict")

            for key in event_properties.keys():
                value = event_properties[key]
                self.add_event_property(key, value)

    def add_event_property(self, name, value):
        # TODO: add check or warning about possibly conflicting
        # event properties used in Amplitude?
        # (e.g.: 'city', 'ip_address', 'device_manufacturer', 'device_type' just to name a few )
        self._data["event_property"][name] = value

    def __repr__(self):
        """Official str representation of the user activity metric."""
        return "%s:%s" % (self._data["event_group"], self._data["event_name"])


###############################################################################
# metrics logging convenience functions

def log_event_metric(metric_event, log_once=False):
    """ Log a Toolkit metric event now using the Amplitude service.

    This method adds the metric event to a dispatch queue, it doesn't get
    posted on the web right away.

    A dispatcher processes the queue every N seconds, it packs queued metrics
    into a single payload and then submit the payload to a Shotgun site.

    :param MetricEvent metric: A metric event to add the the queue.

    :param bool log_once: ``True`` if this metric should be ignored if it has
        already been logged. Defaults to ``False``.

    """
    MetricsQueueSingleton().log(metric_event, log_once=log_once)

def log_metric(metric, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass

def log_user_activity_metric(module, action, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass

def log_user_attribute_metric(attr_name, attr_value, log_once=False):
    """ Depricated method, use the `log_metric_event` method."""
    pass

