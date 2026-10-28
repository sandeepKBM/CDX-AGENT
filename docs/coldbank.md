# cdx-agent --coldbank: big store on westeros, on-demand local caches elsewhere

Shared resource rule R1 (codex_tools/base/AGENTS.md "Shared resource rules"): COLD = `westeros:/data/local/ss5772/coldbank`.
Hosts with little local space (ilabu: /var/tmp quota 10 GB, /data not readable) fetch an item only when a job needs it, verify it,
keep it in a size-budgeted cache, and drop the least recently used unleased copies when room runs out. The bank is the master
copy; cache entries are redundant copies by construction, so evicting one never loses data.

Measured 2026-10-28: westeros -> ilabu over ssh 152 MB/s (default cipher), 260 MB/s (`-c aes128-gcm@openssh.com`), one stream,
westeros CPU-bound (load ~80); NFS on ilabu reads 1.1 GB/s, so parallel streams help.

## Bank layout (one directory per item; existing convention)
```
<bank>/<name>/            files of the item (any tree)
<bank>/<name>/MANIFEST.sha256   `sha256sum` lines, paths relative to the item dir; excludes MANIFEST.sha256, README.txt
<bank>/<name>/README.txt        key=value lines, required: source, owner, regenerable (yes|no), cost
```
Compressed item (`put --zstd`): `<name>/data.tar.zst` + `MANIFEST.sha256` (covers data.tar.zst) + `CONTENTS.sha256` (covers
the files inside the archive, relative paths) + README.txt.

Bank spec: `HOST:PATH` (remote over ssh) or `PATH` (local filesystem, also used by the tests and for NFS banks).
Default `westeros:/data/local/ss5772/coldbank`; override with `--bank` or env `CDX_COLDBANK`. When HOST is the current host
(short hostname match) the bank is local.

## Cache (per host)
Root from `--cache DIR`, env `CDX_COLDCACHE`, or the host table (config `~/.config/cdx-agent/coldbank.json` overrides the built-in):
| host pattern | cache dir | budget |
|---|---|---|
| ilabu | /var/tmp/ss5772/coldcache | 6 GB |
| ilab*, rlab* (Slurm nodes) | /freespace/local/ss5772/coldcache | 200 GB |
| bank host (westeros) | none: `fetch` prints the bank path, no copy | - |
Budget via `--budget-gb`. Unknown host and no `--cache`: refuse (exit 2).

```
<cache>/<name>/                fetched files (same relative paths as in the bank)
<cache>/<name>/.entry.json     {name, bank, files: {rel: sha256}, bytes, created, last_used}
<cache>/.partial/<name>.<pid>/ in-flight downloads (cleaned on failure; stale ones removed by gc)
<cache>/.leases/<name>/<id>.json  {id, pid, host, job, created, expires}
<cache>/.lock                  flock for metadata/eviction; <cache>/.locks/<name>.lock held while fetching that item
<cache>/coldcache.jsonl        machine log: fetch / evict / release / gc events (one JSON object per line)
```

## Commands (`cdx-agent --coldbank <cmd> ...`, module `cdx_agent.coldbank`)
- Remote item metadata (MANIFEST, README, CONTENTS, sizes) is read in ONE ssh call; already-cached files are trusted from
  the sha + size recorded at fetch time (`--rehash` re-hashes). Measured ilabu <- westeros: 5.03 GB fetch+verify 49 s; refetch 2.7 s.
- `list` - bank items: name, files, bytes, compressed?, README source/regenerable/cost. Sizes via remote
  `find <item> -type f -printf '%P\t%s\n'`.
- `put SRC NAME --meta source=... --meta owner=... --meta regenerable=yes|no --meta cost=... [--zstd] [--update]`
  copy SRC (dir or file) into the bank, write MANIFEST.sha256 (+ CONTENTS.sha256 for --zstd) and README.txt, then verify the
  bank copy (`sha256sum -c` on the bank host). Refuses an existing NAME unless --update; refuses missing required meta.
  Never removes SRC. The new copy is verified in a staging dir BEFORE it replaces anything; `--update` keeps the old version
  as `<name>.old.<stamp>` (rule: move, never delete) and prints the line for the user to remove it.
- `fetch NAME [--only REL ...] [--streams N=4] [--lease-hours H=12 | --no-lease] [--job ID] [--dry-run]`
  Ensure NAME (or only the listed files, which must appear in the manifest) is in the local cache and verified; take a lease;
  print the local item path as the LAST stdout line (for `$(...)`). Steps: per-item lock; files already present with matching
  sha are skipped; `need` = bytes still missing; admission: used + need <= budget AND free >= need * 1.05, free = min(statvfs free, room left under the user's block quota
  from `quota -w -v`; ilabu /var/tmp has a 10 GB quota that statvfs does not show), else evict
  (below); download into .partial with N parallel `rsync -a --files-from` streams (files split by size, round robin by bytes),
  `-e "ssh -o BatchMode=yes -c aes128-gcm@openssh.com"` for remote banks (local banks: plain rsync); zstd items stream
  `ssh HOST cat data.tar.zst | zstd -dc | tar -x [members]`; verify sha256 of every new file against the manifest
  (CONTENTS.sha256 for zstd); on any mismatch delete .partial and exit 1 (no entry change); then move files into place
  (os.replace) and update .entry.json. On the bank host: print the bank path, no copy, no lease.
- `run NAME [fetch options] -- CMD...` fetch with a lease, run CMD with env `COLDBANK_PATH=<local item path>`, release the
  lease when CMD exits (also on failure/signal); exit code = CMD's.
- `release NAME [--id ID]` drop this process's/that lease.
- `cat NAME REL` stream one bank file to stdout, hashing while streaming; exit 1 at the end if the sha differs.
- `status` cache root, budget, used bytes, entries (bytes, last_used, live leases).
- `gc [--dry-run]` drop expired/dead leases and stale .partial dirs; evict LRU unleased entries while over budget.
- `verify NAME` run `sha256sum -c --quiet MANIFEST.sha256` (and CONTENTS for zstd after a test extract is NOT done) on the bank.

Leases: `fetch` takes a TIME lease (pid 0; live until `--lease-hours` pass or `release`), because its caller is usually a
short-lived wrapper/subshell; `fetch --pid P` ties it to P instead; `run` takes a pid lease on itself (gone when CMD ends);
in a Slurm job the lease carries `$SLURM_JOB_ID` and lives until it expires. A lease from another host counts until expiry.
`release NAME` drops the time leases, `--pid P` that pid's, `--id ID` one, `--all` every lease of NAME on this host.

Eviction: candidates = entries under the cache root that are real directories (not symlinks) with a valid .entry.json and
no live lease, excluding the item being fetched; oldest `last_used` first, until the fetch fits. Removal = shutil.rmtree of
that entry dir only (the bank keeps the master copy); every eviction is logged. If it still does not fit: exit 3 "no room",
listing what holds the space. `--dry-run` prints the plan only.

Names: `[A-Za-z0-9._-]+`, not starting with '.'; `--only`/`cat` paths must be manifest entries (no '..', not absolute).
Exit codes: 0 ok, 1 transfer/verify failure, 2 refused/bad input, 3 no room.
