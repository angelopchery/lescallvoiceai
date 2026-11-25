# Restoring LesCall

Baseline taken 2026-11-05, before the UI rebuild.

| What | Where |
|---|---|
| All code, configs, migrations, docs | Tag `platform-baseline-2026-11-05` in this repo |
| `.env` and key files, database, agents, Copilot chats and uploads, WhatsApp media, TTS caches, credit log, `/etc` configs, TLS certs, recordings not yet in GCS | Branch `data-backup/2026-11-05`, encrypted |
| Passphrase for the encrypted backup | Owner's password manager (server copy: `~/.lescall-backup-passphrase`) |
| Older recordings (446 MB in total on the server) | `recordings/` on the server, and GCS. **Not in this repo** |

The running containers read `/home/angelo/lescall` through a bind mount, so
changing files there changes what the next restart runs. Check for live calls
first: `ss -tn | grep ':9001 .*ESTAB'`.

## A. A code change went wrong (server still fine)

See what changed, then restore one path or everything:

```bash
git diff --stat platform-baseline-2026-11-05
git restore --source=platform-baseline-2026-11-05 -- path/to/file    # one path
git restore --source=platform-baseline-2026-11-05 -- .               # all tracked files
```

Apply it:

```bash
docker restart lescall-runtime lescall-api lescall-workers lescall-admin
docker compose -f deploy/docker-compose.services.yml up -d --build spa   # if frontend/ changed
```

`git restore` leaves new files in place; `git status` lists them. Agent YAMLs
are mostly gitignored, so agent damage needs section C, or the agent's own
revision history in the product.

## B. Rebuild on a new server

```bash
git clone git@github.com:angelo-dhronai/LesCallVoiceAI.git /home/angelo/lescall
cd /home/angelo/lescall && git checkout -b restored platform-baseline-2026-11-05
```

Then follow section C and restore everything, install FreeSWITCH and nginx, and
start the containers from `deploy/docker-compose.postgres.yml` and
`deploy/docker-compose.services.yml`. `deploy/provision_vm_sa.sh` recreates the
GCS service account. Point the Plivo trunk and DNS at the new IP.

`docs/PLATFORM.md` is the map of what runs where. This path has been tested only
as far as the data restore in section C, not as a full new-server rebuild.

## C. Restore data from the encrypted backup

```bash
mkdir -p ~/restore && chmod 700 ~/restore && cd ~/restore
git clone --depth 1 --single-branch --branch data-backup/2026-11-05 \
  git@github.com:angelo-dhronai/LesCallVoiceAI.git bk
cd bk && sha256sum -c SHA256SUMS && cd ..
cat bk/lescall-data.tar.zst.gpg.part-* \
  | gpg --batch --decrypt --passphrase-file ~/.lescall-backup-passphrase \
  | zstd -d --long=28 | tar -xf -
```

This gives the repo-relative paths (`calling-agent/.env`, `calling-agent/agents/`,
`dashboard/...`, `data/...`, `credit_log/`, `recordings/...`) and `meta/`. Copy
back what you need.

`meta/` holds `lescall-db.sql.gz`, `etc-config.tgz`, `crontab.txt`,
`containers.txt` and `unsynced_recordings.txt`.

**Database.** This replaces all data, so stop the app containers first and be
certain you want it. Keep `ON_ERROR_STOP=1`: without it a failed table can
swallow the tables after it, and the load still exits 0.

```bash
bash deploy/pg_provision_pgvector.sh    # REQUIRED on a new container: the image has no pgvector
docker exec lescall-pg psql -U lescall postgres -c 'DROP DATABASE lescall' -c 'CREATE DATABASE lescall'
gunzip -c meta/lescall-db.sql.gz | docker exec -i lescall-pg psql -q -v ON_ERROR_STOP=1 -U lescall lescall
```

Then compare `select count(*) from calls` with what you expect (1,156 in this
backup). Tested 2026-11-05 on a throwaway container.

**System configs:** `sudo tar -xzf meta/etc-config.tgz -C /` (FreeSWITCH, nginx,
Let's Encrypt, systemd units). **Cron:** `crontab meta/crontab.txt`.

Delete `~/restore` when finished; it holds plaintext secrets and customer data.

## Never do this

- Do not push or merge from the `legacy` remote or `refs/backup/*`. Their history
  contains live API keys and was deliberately left out of this repo.
- Do not commit `.env`, key files or database dumps.
