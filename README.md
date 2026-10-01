# check_podman

A Nagios/Icinga plugin that checks a rootless podman user's containers via
`sudo`. Python 3.7 or newer, standard library only.

It checks that every container is running, reports CPU, memory, network and
block I/O usage of each as performance data, and alerts when a container is
getting close to its memory limit.

## Usage

```sh
check_podman -u eda
check_podman -u eda -n iic-osic-eda-u50001 -n iic-osic-eda-u50002
check_podman -u eda -w 80 -c 95
```

```
PODMAN OK - 2 of 2 container(s) running: db, web | 'web_cpu'=3.12% 'web_mem'=231700000B;;;0;2000000000 'web_mem_percent'=11.31%;80;90 'web_net_in'=4738000B 'web_net_out'=19630000B 'web_block_in'=8589000B 'web_block_out'=147700000B 'web_pids'=81 'db_cpu'=0.41% ...
```

| Option | Meaning |
|---|---|
| `-u`, `--user` | The rootless user owning the containers (required) |
| `-p`, `--podman` | Path of the podman binary (default: `/usr/bin/podman`) |
| `-n`, `--name` | Only check this container; repeatable (default: every container the user has) |
| `-w`, `--warning` | Warning range for memory usage, in percent of the container's memory limit (default: `80`) |
| `-c`, `--critical` | Critical range for memory usage, in percent of the container's memory limit (default: `90`) |
| `-t`, `--timeout` | Timeout in seconds for each podman call (default: 15) |
| `-V`, `--version` | Show the version |

The plugin runs `sudo -u USER podman ps -a --format json` to see every
container the user has (including stopped ones, so a container that isn't
running doesn't just silently disappear from the output), and
`sudo -u USER podman stats --no-stream --format json` for CPU/memory/network/
block I/O of the containers currently running.

### Why `sudo`, not a podman socket

Rootless podman containers live entirely under their owning user's account
(separate storage, separate user namespace), invisible to any other user,
including the monitoring user. `sudo -u USER podman ...` is the simplest way
to look at them from the outside without running the check as that user
directly. It needs two things set up for the rootless user:

- **Lingering**, so the user's runtime directory and cgroups survive without
  an active login session: `loginctl enable-linger USER`
- **A narrow NOPASSWD sudoers rule**, restricted to exactly the two
  read-only subcommands this plugin needs:

  ```
  monitoring ALL=(USER) NOPASSWD: /usr/bin/podman ps *, /usr/bin/podman stats *
  ```

  (replace `monitoring` with whichever account actually runs the check, e.g.
  the user `check_by_ssh` logs in as). No shell access, no `exec`/`run`/`rm`/
  `kill`/`stop` — only listing and stats.

Do **not** invoke with `sudo -u USER -i podman ...` (`-i`, simulate initial
login): depending on the sudo version, that can route the command through
the user's login shell instead of executing it directly, which stops the
sudoers `Command_Spec` from matching and falls back to asking for a
password. Lingering alone is enough for `XDG_RUNTIME_DIR` to resolve
correctly without `-i`.

### Thresholds

`-w` and `-c` take the
[monitoring plugins range format](https://www.monitoring-plugins.org/doc/guidelines.html#THRESHOLDFORMAT)
and apply to each running container's memory usage, as a percentage of its
own memory limit (`podman stats`' `mem_percent`, or computed from
`mem_usage`'s "used / limit" pair if that field isn't present). As a higher
percentage is worse, the defaults `-w 80 -c 90` alert when usage crosses
those percentages. A container that isn't running has no memory stats to
check and is reported as CRITICAL on its own (see States below).

### Performance data

Per running container (labels prefixed with the container name):

- `_cpu`: CPU usage in percent
- `_mem`: memory usage in bytes, with 0 and the memory limit as minimum and
  maximum
- `_mem_percent`: memory usage as percent of the limit, with the thresholds
- `_net_in`/`_net_out`: network I/O in bytes
- `_block_in`/`_block_out`: block I/O in bytes
- `_pids`: number of processes in the container

A stopped container contributes no performance data (podman can't report
stats for a container that isn't running).

### States

- **CRITICAL**: podman cannot be run or does not answer within `-t`, a
  named container (`-n`) doesn't exist, a container is not running, or a
  running container's memory usage is outside `-c`
- **WARNING**: a running container's memory usage is outside `-w`
- **UNKNOWN**: the user has no containers at all, podman's output can't be
  parsed, sudo rejects the command (misconfigured sudoers), or the command
  line is wrong

The plugin prints exactly one line.

## Tests

```sh
python3 -m unittest -v
```

The tests replay recorded `podman ps`/`podman stats` output through a fake
podman and a fake sudo.

## License

GPL-2.0-or-later, see [LICENSE](LICENSE).
