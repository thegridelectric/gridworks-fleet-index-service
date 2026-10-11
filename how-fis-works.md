# How FIS works

What the Fleet Index Service is for and how it keeps the fleet safe,
written for a person meeting it for the first time. The README covers
running and deploying it.

## The problem

Every GridWorks scada, every leaf transactive node (LTN), the market
maker and the platform services talk over one RabbitMQ broker. A message
on that broker can turn a heat pump on or off. Across a hundred homes
that is real load on the grid, so two things must never happen.

1. **Someone who is not part of the fleet dispatches grid assets.** A
   stolen password, a forgotten test script, or a misconfigured
   dashboard should not be able to publish a command that a scada acts
   on.
2. **Two copies of one node run at once.** If the cloud LTN for beech is
   running and someone also starts beech's LTN on a laptop to debug it,
   two brains are dispatching the same scada and bidding for the same
   house. The same goes for a scada left running after a redeploy, or a
   process a restart did not fully kill. Nobody can tell from the data
   which one was real.

The first is about identity: who is this? The second is about instances:
is this the one copy of it that should be talking right now? A
certificate answers the first. FIS answers the second.

## The idea: the broker is primary security 

Nothing in GridWorks dispatches a device except by publishing on the
broker. So the security of the fleet is the question of who may connect
to the broker and what they may publish once connected. Getting in takes
two things.

**A signed certificate.** Each node or service has a private key and a
client certificate signed by the GridWorks certificate authority. The
broker checks the certificate in the TLS handshake before any message
flows, and takes the connection's username from the certificate's common
name (CN). No password is sent. Certificates are cut by one issuance
tool, which registers the identity with FIS in the same step, so a
certificate and its FIS record are created together. A certificate can
be revoked; the broker checks the revocation list at every handshake.

**Belonging to FIS.** The broker then asks FIS, over localhost on the
same box, whether to let this connection in. FIS says yes only if the
identity is one it knows, it is active (not suspended), and this
particular running copy is the one allowed to speak.

A valid certificate alone is not enough, and FIS alone is not enough.
The certificate proves which identity is knocking; FIS decides whether
this copy of it gets in.

## Words

- **Principal**: a durable identity allowed on the broker. Its id is its
  certificate's CN and never changes. Two kinds: a **GNode** is a node in
  the grid topology (a scada, an LTN, a market maker), and its id is its
  GNodeId in the grid node registry; a **Service** is platform
  infrastructure (the registry, the ear, the journalkeeper), and its id
  is a UUID FIS mints.
- **Instance**: one boot of a principal. Every time a process starts it
  makes up a fresh random id, its `GNodeInstanceId`, and keeps it until it
  exits.
- **Run**: one execution of the system against a universe, which is one
  broker vhost (`hw1__1` is production).
- **Lease**: FIS's record that one instance holds the right to speak for
  a principal on a run. There is at most one active lease per principal
  per run.

## How it works: the new instance kicks the old one off

When a connection arrives, FIS looks at the principal and the instance id
it presents and does one of three things.

- **The instance already holds the lease.** This is a reconnect after a
  network blip. It is let straight back in.
- **The instance was kicked off earlier.** It is refused, permanently.
  An old copy can never come back.
- **The instance is new.** This is a fresh boot. FIS revokes the old
  lease, has the broker close every connection the old instance still
  has, waits until the broker confirms they are gone, records the new
  lease, and only then lets the new instance in.

So the newest boot always wins, and two copies never run together. The
newest wins, rather than the first, because the alternative makes a
stuck old process impossible to replace: a restart has to be able to
take over.

Every decision is written down as an authorization event, so every
admission and every refusal can be looked at later. Two processes
fighting over one identity show up as churn in that record instead of as
silent double-reporting.

## A day in the life

**Morning: beech's scada boots.** It has its certificate on disk (CN =
beech's GNodeId). It makes up a new instance id, connects to the broker,
and presents the certificate. The broker validates it and asks FIS. FIS
knows the principal, sees an instance it has never met, finds no old
connection to close, records the lease and says yes. The whole exchange
takes well under a second.

**Beech publishes.** The first time it publishes on a topic, the broker
asks FIS whether the sender in that topic is beech's current alias in the
registry. It is, so the broker allows it and does not ask again for that
topic on that connection.

**A deploy restarts the cloud LTN.** The new process has a new instance
id. FIS kicks off the old process's connections and admits the new one.
This is the normal case and costs nothing.

**Someone starts beech's LTN on a laptop** with a copy of its
certificate. FIS treats it as a new boot: the cloud LTN is kicked off and
the laptop takes over. That is the wrong process winning, but the two
are never dispatching at once, and the takeover is in the record. When
the cloud LTN reconnects with its old instance id it is refused; when its
service manager restarts it with a new id, it takes the lease back and
the laptop is kicked off.

This is why **a certificate belongs to one running program.** Any other
process that presents it, however briefly, kicks the real one off. A
nightly job that ran from the registry's checkout with the registry's
certificate did exactly this: every night it took the registry's lease,
and the registry stayed refused until someone restarted it by hand. The
fix was to run that job inside the registry process. A listener, a test
or an experiment gets its own principal and certificate.

**The registry renames beech** (it moves to a different feeder, so its
alias changes; its certificate does not). FIS learns of the rename from
the registry and has the broker close beech's connections. Beech
reconnects and publishes under its new alias. A copy still using the old
alias gets in but its first publish is refused.

**A certificate is believed stolen.** An operator suspends the principal
in FIS and its connections are closed. Every later attempt is refused at
the door, whatever instance id it presents. Revoking the certificate
follows.

**FIS is down.** The broker cannot get an answer, so it admits no new
connection. Connections already open keep working. FIS fails closed on
purpose: admitting everyone while the authority is away is exactly the
two-brains failure it exists to prevent.

## What a connection may do once it is in

1. **Every principal may read everything.** Reading the fleet is about
   visibility, not authority. A payload that must be confidential is
   encrypted, not hidden.
2. **A GNode may publish only under its own alias.** The sender segment
   of the routing key must be the principal's current alias in the
   registry, so one node cannot speak as another.
3. **A Service may publish on any routing key.** A service has no
   registry alias to pin it to.

Rule 3 is why only the fleet and the platform services belong on the
production broker. A reader that is not part of the fleet (a website, a
dashboard, a partner) gets its own broker fed one way, because on the
production broker any admitted Service could publish a command.

## What FIS is not

No message passes through FIS. It is not a router, not a store of
readings, and not a health monitor: it knows who holds authority, not
whether that process is alive this minute. It issues no certificates and
keeps no certificate state. It reads the registry and answers the broker.
