# Connecting Windows pgAdmin to the local PostgreSQL

The project's PostgreSQL 16 (+ pgvector) runs in **userspace inside WSL** and is
managed by `backend/scripts/pg_local.sh`. It listens on all interfaces on port
**54329**. This guide connects a Windows **pgAdmin** client to it.

> The backend app connects over loopback (`127.0.0.1` / `::1`) using the `trust`
> rule — no password. External clients like pgAdmin connect over the WSL IP and
> must authenticate with a password (`scram-sha-256`).

---

## 1. Quick connection values

| Field             | Value                                                    |
| ----------------- | -------------------------------------------------------- |
| Host              | **172.25.60.4** (the WSL IP — see note below)            |
| Port              | **54329**                                                |
| Maintenance DB    | **ragdb**                                                |
| Username          | **raguser**                                              |
| Password          | **ragpass**                                              |

> **The WSL IP can change** when WSL restarts. Refresh the current value with:
>
> ```bash
> bash backend/scripts/pg_local.sh ip
> ```
>
> Use whatever that prints as the **Host** in pgAdmin.

---

## 2. pgAdmin steps

1. In the pgAdmin browser tree, right-click **Servers > Register > Server…**
2. **General** tab → **Name**: `RAG local`
3. **Connection** tab → fill in:
   - **Host name/address**: `172.25.60.4` (or the current `pg_local.sh ip` value)
   - **Port**: `54329`
   - **Maintenance database**: `ragdb`
   - **Username**: `raguser`
   - **Password**: `ragpass`
   - Tick **Save password**
4. Click **Save**.

> **Tip — `localhost` first:** You can also try **Host = `localhost`**. This works
> only if WSL's localhost-forwarding is active, and that path hits the **loopback
> `trust`** rule, so the password is ignored. It's convenient but not always
> available. The **WSL IP + password** is the most reliable connection.

---

## 3. What you'll see

After connecting, expand the server:

- Databases: **ragdb** (the app database) and **ragdb_test**.
- In `ragdb` → **Schemas → public → Tables**:
  - **documents**
  - **chunks** — note `chunks.embedding` is a **pgvector `Vector(384)`** column
  - **alembic_version**

Example query (open Query Tool on `ragdb`):

```sql
SELECT id, filename, indexing_status, chunk_count FROM documents;
```

---

## 4. Keeping it reachable

- **The WSL IP changes on WSL restart.** When connections start failing, re-fetch
  it and update the pgAdmin server's **Host** (right-click the server →
  **Properties → Connection → Host name/address**):

  ```bash
  bash backend/scripts/pg_local.sh ip
  ```

- **After a machine reboot**, bring the database back up and re-expose it:

  ```bash
  bash backend/scripts/pg_local.sh up
  bash backend/scripts/pg_local.sh expose
  ```

  `expose` is idempotent — it re-applies external access (listen on all
  interfaces, ensure the role password, add the scram host rule).

---

## 5. Troubleshooting

- **Connection refused / timeout**
  - Make sure the server is running: `bash backend/scripts/pg_local.sh up`
  - Re-check the WSL IP: `bash backend/scripts/pg_local.sh ip` — and update the
    pgAdmin Host if it changed.
  - Check **Windows Firewall** isn't blocking the connection to the WSL VM.

- **Authentication failed**
  - The password is **`ragpass`**.
  - Confirm you're connecting via the **WSL IP** (which uses scram/password), not
    `localhost` (loopback `trust`, where the password is ignored).

---

## 6. Security note

The external rule `host all all 0.0.0.0/0 scram-sha-256` allows password auth from
**any host that can reach the WSL VM**. This is intended for **local development
only**. To lock it back down to loopback-only (the app keeps working over the
`trust` rule):

```bash
bash backend/scripts/pg_local.sh unexpose
```

---

## 7. Revert

```bash
bash backend/scripts/pg_local.sh unexpose
```

Reverts to loopback-only: removes the external scram host rule and sets
`listen_addresses='127.0.0.1'`. The backend app continues to work.
