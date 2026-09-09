# Kubernetes

GeoMetrikks is a single container that needs a PostgreSQL/TimescaleDB, a
volume for its GeoLite2 database, and read access to the access logs it
tails. This page is a working set of manifests plus the constraints that
actually matter in a cluster.

Run the database outside the app, either as its own workload or as a
managed TimescaleDB; see [External database](external-database.md) for
what it must provide. The bundled compose database has no Kubernetes
equivalent here on purpose — a stateful database deserves its own
operator or service.

## Constraints worth knowing first

- **One replica.** Sessions, WebSocket fan-out, the scheduler and log
  ingestion are all process-local, and startup migrations are not locked
  across processes. Two replicas means duplicated ingestion and scheduled
  jobs. Use `replicas: 1` with `strategy: Recreate`, and scale reach with
  agents (below), not with copies of the head. The reasoning is in
  [Deployment](deployment.md#the-single-worker-constraint).
- **Startup can be slow.** The first start on a version that changes a
  continuous aggregate re-materializes it before serving anything. Give
  the pod a `startupProbe` with a generous budget instead of letting a
  liveness probe restart it in a loop.
- **The filesystem is written to.** `/app/logs`, `/app/data/geoip` and
  `/app/.litestar.json` are all written at runtime, so
  `readOnlyRootFilesystem: true` does not work as-is.
- **Graceful shutdown takes ~15s.** Ingestion drains, the scheduler stops
  and the CrowdSec client closes on SIGTERM. Keep
  `terminationGracePeriodSeconds` at 30 or more.

## Configuration and secrets

Non-secret settings belong in a ConfigMap, credentials in a Secret. The
app reads every setting from an environment variable, and every one of
them also from a file when you append `_FILE` to the variable name — which
is what makes secret volumes work without an init container.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: geometrikks
data:
  DB_HOST: "timescale.databases.svc.cluster.local"
  DB_PORT: "5432"
  DB_USER: "geouser"
  DB_DATABASE: "geometrikks"
  DB_SSLMODE: "verify-full"
  LOGPARSER_LOG_PATHS: "/var/log/access/access.log"
  LOGPARSER_HOST_NAME: "edge-01"
  APP_SESSION_SECURE: "true"        # served over HTTPS through the ingress
  APP_TRUSTED_PROXIES: "10.0.0.0/8" # the ingress controller's pod CIDR
  MAP_AUTO_DETECT_HOME: "true"
---
apiVersion: v1
kind: Secret
metadata:
  name: geometrikks
type: Opaque
stringData:
  db-password: "change-me"
  admin-password: "change-me"
  maxmind-license-key: "change-me"
```

Mount the Secret and point the `_FILE` variables at the mounted keys. The
values then never appear in the pod spec, in `kubectl describe pod`, or in
the container's environment:

```yaml
          env:
            - name: DB_PASSWORD_FILE
              value: /run/secrets/geometrikks/db-password
            - name: APP_ADMIN_PASSWORD_FILE
              value: /run/secrets/geometrikks/admin-password
            - name: MAXMINDDB_LICENSE_KEY_FILE
              value: /run/secrets/geometrikks/maxmind-license-key
```

The classic `valueFrom.secretKeyRef` works too, and one connection string
covers the whole address at once:

```yaml
            - name: DB_CONNECTION_STRING
              valueFrom:
                secretKeyRef:
                  name: geometrikks
                  key: dsn        # postgresql://user:pass@host:5432/geometrikks?sslmode=require
```

A connection string wins over the individual `DB_*` variables, so a
provider's own secret can be consumed as-is while the ConfigMap keeps the
rest.

To verify the database certificate against a private CA, mount it and set
`DB_SSLROOTCERT` to the mounted path; without it, `verify-ca` and
`verify-full` use the system trust store.

## The deployment

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: geometrikks-geoip
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: geometrikks
spec:
  replicas: 1          # see "Constraints" above; more is not a tuning knob
  strategy:
    type: Recreate     # never two writers, and RWO volumes only attach once
  selector:
    matchLabels:
      app: geometrikks
  template:
    metadata:
      labels:
        app: geometrikks
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        # The image's user is uid/gid 1000 and owns /app; running as it
        # directly skips the entrypoint's root phase entirely. fsGroup makes
        # the mounted volumes writable for it.
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: geometrikks
          image: ghcr.io/gilbn/geometrikks:0.14.3   # pin; :latest for the newest stable
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - configMapRef:
                name: geometrikks
          env:
            - name: DB_PASSWORD_FILE
              value: /run/secrets/geometrikks/db-password
            - name: APP_ADMIN_PASSWORD_FILE
              value: /run/secrets/geometrikks/admin-password
            - name: MAXMINDDB_LICENSE_KEY_FILE
              value: /run/secrets/geometrikks/maxmind-license-key
            - name: MAXMINDDB_USER_ID
              value: "123456"
          volumeMounts:
            - name: secrets
              mountPath: /run/secrets/geometrikks
              readOnly: true
            - name: geoip
              mountPath: /app/data/geoip
            - name: logs
              mountPath: /app/logs
            - name: access-logs
              mountPath: /var/log/access
              readOnly: true
          startupProbe:
            # Answers 200 as soon as the server accepts, which is after
            # migrations and any aggregate re-materialization: 10 minutes of
            # budget covers a large upgrade without a restart loop.
            httpGet: { path: /health, port: http }
            periodSeconds: 10
            failureThreshold: 60
          readinessProbe:
            # 503 while the database is unreachable, so the pod leaves the
            # Service instead of serving a degraded UI.
            httpGet: { path: /health/ready, port: http }
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /health, port: http }
            periodSeconds: 30
            failureThreshold: 3
          resources:
            requests: { cpu: 100m, memory: 512Mi }
            limits: { memory: 2Gi }
      volumes:
        - name: secrets
          secret:
            secretName: geometrikks
            defaultMode: 0400
        - name: geoip
          persistentVolumeClaim:
            claimName: geometrikks-geoip
        - name: logs
          emptyDir: {}
        - name: access-logs
          # Whatever carries your proxy's access log; see below.
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: geometrikks
spec:
  selector:
    app: geometrikks
  ports:
    - name: http
      port: 80
      targetPort: http
```

`/app/logs` holds the application log and `login.log`. Both also go to
stdout, so an `emptyDir` is enough unless a host-side tool (fail2ban,
CrowdSec) reads the files.

### Ingress

Nothing special, other than WebSocket support for the live map and feed —
which ingress-nginx and Traefik do by default:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: geometrikks
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"   # keep /ws/live open
spec:
  rules:
    - host: metrics.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: geometrikks
                port:
                  name: http
  tls:
    - hosts: ["metrics.example.com"]
      secretName: geometrikks-tls
```

Set `APP_SESSION_SECURE=true` behind TLS, and list the ingress
controller's pod CIDR in `APP_TRUSTED_PROXIES` so login logging records
the real client address instead of the proxy's.

## Getting the access logs in

The app tails files, so the log has to reach its filesystem. Three shapes,
in the order they usually make sense in a cluster:

1. **An agent next to the proxy.** Run a second GeoMetrikks with
   `APP_MODE=agent` as a sidecar in the proxy's pod (or a DaemonSet on the
   proxy's nodes), sharing an `emptyDir` with it. The agent tails, parses,
   geolocates and writes to the same database; the head serves the UI and
   never sees the file. This is the shape that survives more than one
   proxy — see [Multi-source setup](../README.md#multi-source-setup).

   ```yaml
        - name: geometrikks-agent
          image: ghcr.io/gilbn/geometrikks:0.14.3   # same tag as the head
          env:
            - name: APP_MODE
              value: agent
            - name: LOGPARSER_HOST_NAME
              value: edge-01
            - name: LOGPARSER_LOG_PATHS
              value: /var/log/access/access.log
          envFrom:
            - configMapRef:
                name: geometrikks     # same database address
          volumeMounts:
            - name: access-logs
              mountPath: /var/log/access
              readOnly: true
   ```

   An agent serves only `/health` and `/health/ready`, needs no admin
   password, and downloads its own GeoLite2 database, so give it a geoip
   volume and the MaxMind credentials.

2. **A shared ReadWriteMany volume.** If your proxy already writes its
   access log to an NFS/CephFS-backed PVC, mount the same claim read-only
   into the GeoMetrikks pod.

3. **A log shipper writing a file.** Anything that lands nginx, Traefik or
   Caddy JSON lines in a file the pod can read works; point
   `LOGPARSER_LOG_PATHS` at it.

A controller that only logs to stdout (ingress-nginx's default) has to be
configured to write a file first — otherwise there is nothing to tail.

## Migrations as their own step

The container migrates at startup by default, which is usually what you
want with a single replica. To gate a rollout on the schema instead, set
`DB_MIGRATE_ON_STARTUP=false` in the ConfigMap and run a Job first:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: geometrikks-migrate
spec:
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
      containers:
        - name: migrate
          image: ghcr.io/gilbn/geometrikks:0.14.3
          args: ["litestar", "database", "upgrade", "--no-prompt"]
          envFrom:
            - configMapRef:
                name: geometrikks
          env:
            - name: DB_PASSWORD_FILE
              value: /run/secrets/geometrikks/db-password
            - name: APP_ADMIN_PASSWORD_FILE
              value: /run/secrets/geometrikks/admin-password
          volumeMounts:
            - name: secrets
              mountPath: /run/secrets/geometrikks
              readOnly: true
      volumes:
        - name: secrets
          secret:
            secretName: geometrikks
```

Composing the app still needs the normal environment, `APP_ADMIN_PASSWORD`
included, even though no server starts. TimescaleDB objects are still
configured at app startup and expect the schema at head, so run the Job as
a pre-install/pre-upgrade hook or an init container, not afterwards.

## Upgrades

Pin an exact tag and bump it deliberately; `Recreate` stops the old pod
before the new one starts, so there is no window with two writers. Watch
the new pod's logs on a version that touches continuous aggregates: it
answers nothing until the re-materialization finishes, which is why the
`startupProbe` budget is generous.
