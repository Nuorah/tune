# tune

`tune` is a command-line music client for Subsonic-compatible servers such as Navidrome. It uses a small background daemon to retain an authenticated session and control playback across separate command invocations.

I built tune because I wanted a CLI music client that fit my Linux desktop workflow without storing my Navidrome credentials in a plaintext configuration file. Rather than implementing audio playback, I chose to build around mpv, using a background daemon to maintain playback state and support integration with tools such as Waybar and external search interfaces.

The client is designed around a simple workflow:

```console
$ tune connect https://music.example.com --username alice
Password:
Connected to https://music.example.com as alice

$ tune albums --limit 10
 1  Artist — Album (2024, 10 tracks)
 2  Other Artist — Other Album (2023, 8 tracks)

$ tune play 2
Playing Other Artist — Other Album (8 tracks); first: First Track

$ tune pause
Paused

$ tune disconnect
Disconnected
```

## AI disclosure

The initial codebase and this README were written by an AI coding assistant under human direction and review.

## Requirements

- Python 3.11 or newer
- [mpv](https://mpv.io/) available in `PATH`
- A Subsonic-compatible server available over HTTPS
- A Unix-like operating system with Unix-domain socket support

## Installation

### Install with uv

Clone the repository, enter it, and install the command:

```console
git clone <repository-url>
cd tune
uv tool install .
```

Confirm that the command is available:

```console
tune --help
```

To reinstall after updating the source:

```console
uv tool install --force .
```

To uninstall:

```console
uv tool uninstall tune-subsonic
```

### Editable installation

For development, install an editable version:

```console
uv tool install --editable .
```

Changes to the source tree will then be used without reinstalling the tool. A running daemon must still be restarted to load changed code:

```console
tune disconnect
tune connect https://music.example.com --username alice
```

## Usage

### Connect

Start the daemon and establish an authenticated session:

```console
tune connect https://music.example.com
```

This prompts for both username and password. A username may be supplied as an argument:

```console
tune connect https://music.example.com --username alice
```

The password cannot be supplied as a command-line argument or through redirected standard input.

### List albums

List albums alphabetically:

```console
tune albums
```

List recently added albums first:

```console
tune albums --newest
```

Limit or paginate the results:

```console
tune albums --limit 25
tune albums --offset 25 --limit 25
```

The displayed numbers are retained in daemon memory. Running `albums` or `search` replaces the previous numbered result set.

### Search albums

Search through the server's Subsonic `search3` endpoint:

```console
tune search radiohead
tune search dark side of the moon
tune search "dark side of the moon" --limit 20
```

Matching and ranking are determined by the server.

### Play an album

Play an album from the most recent `albums` or `search` results:

```console
tune play 3
```

The album queue is sent to a headless mpv process through randomized loopback proxy URLs. Authenticated server URLs remain inside the daemon.

### Playback controls

```console
tune pause
tune resume
tune toggle
tune volume 50
tune next
tune previous
tune stop
```

`toggle` switches between paused and playing states. `volume` sets Tune's mpv software volume from 0 to 100 without changing the system volume. Playback starts at 50% by default, and a changed value is retained in daemon memory until disconnect. `stop` stops playback but leaves the authenticated session and daemon running.

### Status

Show connection and playback status:

```console
tune status
```

Example:

```text
Connected to https://music.example.com as alice
Playing 3/10: Artist — Track Title (Album) [1:42 / 4:15]
```

### Waybar

Print one Waybar-compatible JSON status object:

```console
tune waybar
```

The object includes display text, a tooltip, a CSS class for the current state, and playback percentage when timing information is available. Classes are `playing`, `paused`, `stopped`, and `disconnected`.

For frequent updates, use watch mode. It keeps one process running and emits newline-delimited JSON instead of starting a new Python process for every update:

```console
tune waybar --watch
tune waybar --watch --interval 0.5
```

The default watch interval is 0.25 seconds. A Waybar custom module can consume it directly:

```json
{
    "custom/tune": {
        "exec": "tune waybar --watch",
        "return-type": "json",
        "escape": true,
        "on-click": "tune toggle"
    }
}
```

### Disconnect

Stop playback, terminate mpv and the local proxy, discard credentials, and stop the daemon:

```console
tune disconnect
```

The daemon and its in-memory credentials also disappear when the computer shuts down.

## Development

Run the command directly from the source tree without installing it:

```console
PYTHONPATH=src python -m tune.cli --help
```

Run the test suite:

```console
PYTHONPATH=src python -m unittest discover -s tests -v
```

The tests use Python's standard `unittest` library. Network-facing tests use fake Subsonic responses or a temporary loopback server. mpv process tests use mocks and do not produce audio.

Project structure:

```text
src/tune/cli.py        command parsing and user-facing output
src/tune/daemon.py     session state and request routing
src/tune/player.py     loopback stream proxy and mpv control
src/tune/protocol.py   Unix-socket JSON framing
src/tune/runtime.py    secure ephemeral runtime paths
src/tune/subsonic.py   Subsonic API and authenticated streaming
tests/test_tune.py     unit and integration tests
```

The project currently has no runtime Python dependencies outside the standard library.

## TODO

1. Add additional playback modes and controls, including repeat one, repeat all, and random playback.
2. Add playlist creation and management.
3. Add local file playback.
4. Add support for other server protocols.
5. Add native MPRIS integration for `playerctl` and desktop media controls.
6. Rewrite in Rust then in Zig

## Public domain

This project is dedicated to the public domain under the CC0 1.0 Universal Public Domain Dedication. See `LICENSE` for the full terms.
