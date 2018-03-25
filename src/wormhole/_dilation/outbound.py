from __future__ import print_function, unicode_literals
from collections import deque
from attr import attrs, attrib
from attr.validators import provides
from zope.interface import implementer
from .._interfaces import IDilationManager, IOutbound


# Outbound flow control: app writes to subchannel, we write to Connection

# The app can register an IProducer of their choice, to let us throttle their
# outbound data. Not all subchannels will have producers registered, and the
# producer probably won't be the IProtocol instance (it'll be something else
# which feeds data out through the protocol, like a t.p.basic.FileSender). If
# a producerless subchannel writes too much, we won't be able to stop them,
# and we'll keep writing records into the Connection even though it's asked
# us to pause. Likewise, when the connection is down (and we're busily trying
# to reestablish a new one), registered subchannels will be paused, but
# unregistered ones will just dump everything in _outbound_queue, and we'll
# consume memory without bound until they stop.

# We need several things:
#
# * Add each registered IProducer to a list, whose order remains stable. We
#   want fairness under outbound throttling: each time the outbound
#   connection opens up (our resumeProducing method is called), we should let
#   just one producer have an opportunity to do transport.write, and then we
#   should pause them again, and not come back to them until everyone else
#   has gotten a turn. So we want an ordered list of producers to track this
#   rotation.
#
# * Remove the IProducer if/when the protocol uses unregisterProducer
#
# * Remove any registered IProducer when the associated Subchannel is closed.
#   This isn't a problem for normal transports, because usually there's a
#   one-to-one mapping from Protocol to Transport, so when the Transport you
#   forget the only reference to the Producer anyways. Our situation is
#   unusual because we have multiple Subchannels that get merged into the
#   same underlying Connection: each Subchannel's Protocol can register a
#   producer on the Subchannel (which is an ITransport), but that adds it to
#   a set of Producers for the Connection (which is also an ITransport). So
#   if the Subchannel is closed, we need to remove its Producer (if any) even
#   though the Connection remains open.
#
# * Register ourselves as an IPushProducer with each successive Connection
#   object. These connections will come and go, but there will never be more
#   than one. When the connection goes away, pause all our producers. When a
#   new one is established, write all our queued messages, then unpause our
#   producers as we would in resumeProducing.
#
# * Inside our resumeProducing call, we'll cycle through all producers,
#   calling their individual resumeProducing methods one at a time. If they
#   write so much data that the Connection pauses us again, we'll find out
#   because our pauseProducing will be called inside that loop. When that
#   happens, we need to stop looping. If we make it through the whole loop
#   without being paused, then all subchannel Producers are left unpaused,
#   and are free to write whenever they want. During this loop, some
#   Producers will be paused, and others will be resumed
#
# * If our pauseProducing is called, all Producers must be paused, and a flag
#   should be set to notify the resumeProducing loop to exit
#
# * In between calls to our resumeProducing method, we're in one of two
#   states.
#   * If we're writing data too fast, then we'll be left in the "paused"
#     state, in which all Subchannel producers are paused, and the aggregate
#     is paused too (our Connection told us to pauseProducing and hasn't yet
#     told us to resumeProducing). In this state, activity is driven by the
#     outbound TCP window opening up, which calls resumeProducing and allows
#     (probably just) one message to be sent. We receive pauseProducing in
#     the middle of their transport.write, so the loop exits early, and the
#     only state change is that some other Producer should get to go next
#     time.
#   * If we're writing too slowly, we'll be left in the "unpaused" state: all
#     Subchannel producers are unpaused, and the aggregate is unpaused too
#     (resumeProducing is the last thing we've been told). In this satte,
#     activity is driven by the Subchannels doing a transport.write, which
#     queues some data on the TCP connection (and then might call
#     pauseProducing if it's now full).
#
# * We want to guard against:
#
#   * application protocol registering a Producer without first unregistering
#     the previous one
#
#   * application protocols writing data despite being told to pause
#     (Subchannels without a registered Producer cannot be throttled, and we
#     can't do anything about that, but we must also handle the case where
#     they give us a pause switch and then proceed to ignore it)
#
#   * our Connection calling resumeProducing or pauseProducing without an
#     intervening call of the other kind
#
#   * application protocols that don't handle a resumeProducing or
#     pauseProducing call without an intervening call of the other kind (i.e.
#     we should keep track of the last thing we told them, and not repeat
#     ourselves)
#
# * If the Wormhole is closed, all Subchannels should close. This is not our
#   responsibility: it lives in (Manager? Inbound?)
#
# * If we're given an IPullProducer, we should keep calling its
#   resumeProducing until it runs out of data. We still want fairness, so we
#   won't call it a second time until everyone else has had a turn.


# There are a couple of different ways to approach this. The one I've
# selected is:
#
# * keep a dict that maps from Subchannel to Producer, which only contains
#   entries for Subchannels that have registered a producer. We use this to
#   remove Producers when Subchannels are closed
#
# * keep a Deque of Producers. This represents the fair-throttling rotation:
#   the left-most item gets the next upcoming turn, and then they'll be moved
#   to the end of the queue.
#
# * keep a set of IPushProducers which are paused, a second set of
#   IPushProducers which are unpaused, and a third set of IPullProducers
#   (which are always left paused) Enforce the invariant that these three
#   sets are disjoint, and that their union equals the contents of the deque.
#
# * keep a "paused" flag, which is cleared upon entry to resumeProducing, and
#   set upon entry to pauseProducing. The loop inside resumeProducing checks
#   this flag after each call to producer.resumeProducing, to sense whether
#   they used their turn to write data, and if that write was large enough to
#   fill the TCP window. If set, we break out of the loop. If not, we look
#   for the next producer to unpause. The loop finishes when all producers
#   are unpaused (evidenced by the two sets of paused producers being empty)
#
# * the "paused" flag also determines whether new IPushProducers are added to
#   the paused or unpaused set (IPullProducers are always added to the
#   pull+paused set). If we have any IPullProducers, we're always in the
#   "writing data too fast" state.

# other approaches that I didn't decide to do at this time (but might use in
# the future):
#
# * use one set instead of two. pros: fewer moving parts. cons: harder to
#   spot decoherence bugs like adding a producer to the deque but forgetting
#   to add it to one of the
#
# * use zero sets, and keep the paused-vs-unpaused state in the Subchannel as
#   a visible boolean flag. This conflates Subchannels with their associated
#   Producer (so if we went this way, we should also let them track their own
#   Producer). Our resumeProducing loop ends when 'not any(sc.paused for sc
#   in self._subchannels_with_producers)'. Pros: fewer subchannel->producer
#   mappings lying around to disagree with one another. Cons: exposes a bit
#   too much of the Subchannel internals


@attrs
@implementer(IOutbound)
class Outbound(object):
    # Manage outbound data: subchannel writes to us, we write to transport
    _manager = attrib(validator=provides(IDilationManager))

    def __attrs_post_init__(self):
        # _outbound_queue holds all messages we've ever sent but not retired
        self._outbound_queue = deque()
        self._next_outbound_seqnum = 0
        # _queued_unsent are messages to retry with our new connection
        self._queued_unsent = deque()

        # outbound flow control: the Connection throttles our writes
        self._subchannel_producers = {} # Subchannel -> IProducer
        self._paused = True # our Connection called our pauseProducing
        self._all_producers = deque() # rotates, left-is-next
        self._pull_producers = set()
        self._paused_push_producers = set()
        self._unpaused_push_producers = set()

    def _check_invariants(self):
        assert self._pull_producers.isdisjoint(self._unpaused_push_producers)
        assert self._pull_producers.isdisjoint(self._paused_push_producers)
        assert self._unpaused_push_producers.isdisjoint(self._paused_push_producers)
        assert (self._pull_producers
                .union(self._paused_push_producers)
                .union(self._unpaused_push_producers) ==
                set(self._all_producers))

    def build_record(self, record_type, *args):
        seqnum = self._next_outbound_seqnum
        self._next_outbound_seqnum += 1
        r = record_type(seqnum, *args)
        assert hasattr(r, "seqnum"), r # only Open/Data/Close
        return r

    def queue_record(self, r):
        self._outbound_queue.append(r)

    # our subchannels call these to register a producer

    def subchannel_registerProducer(self, sc, producer, streaming):
        # streaming==True: IPushProducer (pause/resume)
        # streaming==False: IPullProducer (just resume)
        if sc in self._subchannel_producers:
            if self.producer:
                raise ValueError(
                    "registering producer %s before previous one (%s) was "
                    "unregistered" % (producer,
                                      self._subchannel_producers[sc]))
        self._subchannel_producers[sc] = producer
        self._all_producers.append(producer)
        if streaming:
            if self._paused:
                self._paused_push_producers.add(producer)
            else:
                self._unpaused_producers.add(producer)
        else:
            self._pull_producers.add(producer)
        self._check_invariants()

        if self._paused:
            # IPushProducers need to be paused immediately, before they speak
            if streaming:
                producer.pauseProducing() # you wake up sleeping
            # IPullProducers aren't notified until they can write something
        else:
            # IPushProducers set their own pace if we let them, but
            # IPullProducers hit the ground running
            if not streaming:
                producer.resumeProducing() # you wake up screaming

    def subchannel_unregisterProducer(self, sc):
        # TODO: what if the subchannel closes, so we unregister their
        # producer for them, then the application reacts to connectionLost
        # with a duplicate unregisterProducer?
        p = self._subchannel_producers.pop(sc)
        self._all_producers.remove(p)
        self._pull_producers.discard(p)
        self._paused_push_producers.discard(p)
        self._unpaused_producers.discard(p)
        self._check_invariants()

    def subchannel_closed(self, scid, sc):
        self._check_invariants()
        if sc in self._subchannel_producers:
            self.subchannel_unregisterProducer(sc)

    # our Manager tells us when we've got a new Connection to work with

    def use_connection(self, c):
        assert not self._queued_unsent
        self._queued_unsent.extend(self._outbound_queue)
        # the connection can tell us to pause when we send too much data
        c.registerProducer(self, True) # IPushProducer: pause+resume
        # send our queued messages
        self.resumeProducing()

    def stop_using_connection(self):
        self._queued_unsent.clear()
        self.pauseProducing()
        # TODO: I expect this will call pauseProducing twice: the first time
        # when we get stopProducing (since we're registere with the
        # underlying connection as the producer), and again when the manager
        # notices the connectionLost and calls our _stop_using_connection

    def handle_ack(self, resp_seqnum):
        # we've received an inbound ack, so retire something
        while (self._outbound_queue and
               self._outbound_queue[0].seqnum <= resp_seqnum):
            self._outbound_queue.popleft()
        while (self._queued_unsent and
               self._queued_unsent[0].seqnum <= resp_seqnum):
            self._queued_unsent.popleft()
        # Inbound is responsible for tracking the high watermark and deciding
        # whether to ignore inbound messages or not


    # IProducer: the active connection calls these because we used
    # c.registerProducer to ask for them
    def pauseProducing(self):
        if self._paused:
            return # someone is confused and called us twice
        self._paused = True
        for p in self._all_producers:
            if p in self._unpaused_push_producers:
                self._unpaused_push_producers.pop(p)
                self._paused_push_producers.add(p)
                p.pauseProducing()

    def resumeProducing(self):
        if not self._paused:
            return # someone is confused and called us twice
        self._paused = False

        while not self._paused:
            if self._queued_unsent:
                r = self._queued_unsent.popleft()
                self._manager._send_record(r)
                continue
            p = self._get_next_unpaused_producer()
            if not p:
                break
            p.resumeProducing()

    def _get_next_unpaused_producer(self):
        self._check_invariants()
        if not self._pull_producers and not self._paused_push_producers:
            return None
        while True:
            p = self._all_producers[0]
            self._all_producers.rotate(-1) # p moves to the end of the line
            if p in self._pull_producers or p in self._paused_push_producers:
                return p

    def stopProducing(self):
        # we'll hopefully have a new connection to work with in the future,
        # so we don't shut anything down. We do pause everyone, though.
        self.pauseProducing()
