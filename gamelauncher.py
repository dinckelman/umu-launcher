#!/usr/bin/env python3

import os
import argparse
from argparse import ArgumentParser, Namespace
import sys
from pathlib import Path
import tomllib
from typing import Dict, Any, List, Set, Union
import gamelauncher_plugins
from re import match
import subprocess


def parse_args() -> Union[Namespace, str]:  # noqa: D103
    opt_args: Set[str] = {"--help", "-h", "--config"}
    exe: str = Path(__file__).name
    usage: str = f"""
example usage:
  WINEPREFIX= GAMEID= PROTONPATH= {exe} /home/foo/example.exe
  WINEPREFIX= GAMEID= PROTONPATH= {exe} /home/foo/example.exe -opengl
  WINEPREFIX= GAMEID= PROTONPATH= {exe} ""
  WINEPREFIX= GAMEID= PROTONPATH= PROTON_VERB= {exe} /home/foo/example.exe
  WINEPREFIX= GAMEID= PROTONPATH= STORE= {exe} /home/foo/example.exe
  {exe} --config /home/foo/example.toml
    """
    parser: ArgumentParser = argparse.ArgumentParser(
        description="Unified Linux Wine Game Launcher",
        epilog=usage,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--config", help="path to TOML file")

    if not sys.argv[1:]:
        err: str = "Please see project README.md for more info and examples.\nhttps://github.com/Open-Wine-Components/ULWGL-launcher"
        parser.print_help()
        raise SystemExit(err)

    if sys.argv[1:][0] in opt_args:
        return parser.parse_args(sys.argv[1:])

    return sys.argv[1:][0]


def setup_pfx(path: str) -> None:
    """Create a symlink to the WINE prefix and tracked_files file."""
    if not (Path(path + "/pfx")).expanduser().is_symlink():
        # When creating the symlink, we want it to be in expanded form when passed unexpanded paths
        # Example: pfx -> /home/.wine
        # NOTE: When parsing a config file, an error can be raised if the prefix doesn't already exist
        Path(path + "/pfx").expanduser().symlink_to(Path(path).expanduser())
    Path(path + "/tracked_files").expanduser().touch()


def check_env(env: Dict[str, str]) -> Dict[str, str]:
    """Before executing a game, check for environment variables and set them.

    WINEPREFIX, GAMEID and PROTONPATH are strictly required.
    """
    if "WINEPREFIX" not in os.environ:
        err: str = "Environment variable not set or not a directory: WINEPREFIX"
        raise ValueError(err)

    if not Path(os.environ["WINEPREFIX"]).expanduser().is_dir():
        Path(os.environ["WINEPREFIX"]).mkdir(parents=True)
    env["WINEPREFIX"] = os.environ["WINEPREFIX"]

    if "GAMEID" not in os.environ:
        err: str = "Environment variable not set: GAMEID"
        raise ValueError(err)
    env["GAMEID"] = os.environ["GAMEID"]

    if (
        "PROTONPATH" not in os.environ
        or not Path(os.environ["PROTONPATH"]).expanduser().is_dir()
    ):
        err: str = "Environment variable not set or not a directory: PROTONPATH"
        raise ValueError(err)
    env["PROTONPATH"] = os.environ["PROTONPATH"]
    env["STEAM_COMPAT_INSTALL_PATH"] = os.environ["PROTONPATH"]

    return env


def set_env(env: Dict[str, str], args: Union[Namespace, str]) -> Dict[str, str]:
    """Set various environment variables for the Steam RT.

    Filesystem paths will be formatted and expanded as POSIX
    """
    verbs: Set[str] = {
        "waitforexitandrun",
        "run",
        "runinprefix",
        "destroyprefix",
        "getcompatpath",
        "getnativepath",
    }

    # PROTON_VERB
    # For invalid Proton verbs, just assign the waitforexitandrun
    if "PROTON_VERB" in os.environ and os.environ["PROTON_VERB"] in verbs:
        env["PROTON_VERB"] = os.environ["PROTON_VERB"]
    else:
        env["PROTON_VERB"] = "waitforexitandrun"

    # EXE
    # Empty string for EXE will be used to create a prefix
    if isinstance(args, str) and not args:
        env["EXE"] = ""
        env["STEAM_COMPAT_INSTALL_PATH"] = ""
        env["PROTON_VERB"] = "waitforexitandrun"
    elif isinstance(args, str):
        env["EXE"] = Path(args).expanduser().as_posix()
        env["STEAM_COMPAT_INSTALL_PATH"] = Path(env["EXE"]).parent.as_posix()
    else:
        # Config branch
        env["EXE"] = Path(env["EXE"]).expanduser().as_posix()
        env["STEAM_COMPAT_INSTALL_PATH"] = Path(env["EXE"]).parent.as_posix()

    if "STORE" in os.environ:
        env["STORE"] = os.environ["STORE"]

    # ULWGL_ID
    env["ULWGL_ID"] = env["GAMEID"]
    env["STEAM_COMPAT_APP_ID"] = "0"

    if match(r"^ulwgl-[\d\w]+$", env["ULWGL_ID"]):
        env["STEAM_COMPAT_APP_ID"] = env["ULWGL_ID"][env["ULWGL_ID"].find("-") + 1 :]
    env["SteamAppId"] = env["STEAM_COMPAT_APP_ID"]
    env["SteamGameId"] = env["SteamAppId"]

    # PATHS
    env["WINEPREFIX"] = Path(env["WINEPREFIX"]).expanduser().as_posix()
    env["PROTONPATH"] = Path(env["PROTONPATH"]).expanduser().as_posix()
    env["STEAM_COMPAT_DATA_PATH"] = env["WINEPREFIX"]
    env["STEAM_COMPAT_SHADER_PATH"] = env["STEAM_COMPAT_DATA_PATH"] + "/shadercache"
    env["STEAM_COMPAT_TOOL_PATHS"] = (
        env["PROTONPATH"] + ":" + Path(__file__).parent.as_posix()
    )
    env["STEAM_COMPAT_MOUNTS"] = env["STEAM_COMPAT_TOOL_PATHS"]

    return env


def set_env_toml(env: Dict[str, str], args: Namespace) -> Dict[str, str]:
    """Read a TOML file then sets the environment variables for the Steam RT.

    In the TOML file, certain keys map to Steam RT environment variables. For example:
          proton -> $PROTONPATH
          prefix -> $WINEPREFIX
          game_id -> $GAMEID
          exe -> $EXE
    At the moment we expect the tables: 'ulwgl'
    """
    toml: Dict[str, Any] = None
    path_config: str = Path(getattr(args, "config", None)).expanduser().as_posix()

    if not Path(path_config).is_file():
        msg: str = "Path to configuration is not a file: " + getattr(
            args, "config", None
        )
        raise FileNotFoundError(msg)

    with Path(path_config).open(mode="rb") as file:
        toml = tomllib.load(file)

    if not (
        Path(toml["ulwgl"]["prefix"]).expanduser().is_dir()
        or Path(toml["ulwgl"]["proton"]).expanduser().is_dir()
    ):
        err: str = "Value for 'prefix' or 'proton' in TOML is not a directory."
        raise NotADirectoryError(err)

    # Set the values read from TOML to environment variables
    # If necessary, raise an error on invalid inputs
    for key, val in toml["ulwgl"].items():
        # Handle cases for empty values
        if not val and isinstance(val, str):
            err: str = f'Value is empty for key in TOML: {key}\nPlease specify a value or remove the following entry:\n{key} = "{val}"'
            raise ValueError(err)
        if key == "prefix":
            env["WINEPREFIX"] = val
        elif key == "game_id":
            env["GAMEID"] = val
        elif key == "proton":
            env["PROTONPATH"] = val
            env["STEAM_COMPAT_INSTALL_PATH"] = val
        elif key == "store":
            env["STORE"] = val
        elif key == "exe":
            # Raise an error for executables that do not exist
            # One case this can happen is when game options are appended at the end of the exe
            if not Path(val).expanduser().is_file():
                err: str = "Value for key 'exe' in TOML is not a file."
                raise FileNotFoundError(err)

            # It's possible for users to pass values to --options
            # Add any if they exist
            if toml.get("ulwgl").get("launch_args"):
                env["EXE"] = val + " " + " ".join(toml.get("ulwgl").get("launch_args"))
            else:
                env["EXE"] = val

            if getattr(args, "options", None):
                # Assume space separated options and just trust it
                env["EXE"] = (
                    env["EXE"]
                    + " "
                    + " ".join(getattr(args, "options", None).split(" "))
                )

    return env


def build_command(env: Dict[str, str], command: List[str]) -> List[str]:
    """Build the command to be executed."""
    paths: List[Path] = [
        Path(Path().home().as_posix() + "/.local/share/ULWGL/ULWGL"),
        Path(Path(__file__).cwd().as_posix() + "/ULWGL"),
    ]
    entry_point: str = ""
    verb: str = env["PROTON_VERB"]

    # Find the ULWGL script in $HOME/.local/share then cwd
    for path in paths:
        if path.is_file():
            entry_point = path.as_posix()
            break

    # Raise an error if the _v2-entry-point cannot be found
    if not entry_point:
        home: str = Path().home().as_posix()
        dir: str = Path(__file__).cwd().as_posix()
        msg: str = (
            f"Path to _v2-entry-point cannot be found in: {home}/.local/share or {dir}"
        )
        raise FileNotFoundError(msg)

    if not Path(env.get("PROTONPATH") + "/proton").is_file():
        err: str = "The following file was not found in PROTONPATH: proton"
        raise FileNotFoundError(err)

    command.extend([entry_point, "--verb", verb, "--"])
    command.extend(
        [Path(env.get("PROTONPATH") + "/proton").as_posix(), verb, env.get("EXE")]
    )

    return command


def main() -> None:  # noqa: D103
    env: Dict[str, str] = {
        "WINEPREFIX": "",
        "GAMEID": "",
        "PROTON_CRASH_REPORT_DIR": "/tmp/ULWGL_crashreports",
        "PROTONPATH": "",
        "STEAM_COMPAT_APP_ID": "",
        "STEAM_COMPAT_TOOL_PATHS": "",
        "STEAM_COMPAT_LIBRARY_PATHS": "",
        "STEAM_COMPAT_MOUNTS": "",
        "STEAM_COMPAT_INSTALL_PATH": "",
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": "",
        "STEAM_COMPAT_DATA_PATH": "",
        "STEAM_COMPAT_SHADER_PATH": "",
        "FONTCONFIG_PATH": "",
        "EXE": "",
        "SteamAppId": "",
        "SteamGameId": "",
        "STEAM_RUNTIME_LIBRARY_PATH": "",
        "STORE": "",
        "PROTON_VERB": "",
    }
    command: List[str] = []
    # Represents a valid list of current supported Proton verbs
    args: Union[Namespace, str] = parse_args()

    if isinstance(args, Namespace):
        set_env_toml(env, args)
    else:
        check_env(env)

    setup_pfx(env["WINEPREFIX"])
    set_env(env, args)

    # Game Drive
    gamelauncher_plugins.enable_steam_game_drive(env)

    # Set all environment variables
    # NOTE: `env` after this block should be read only
    for key, val in env.items():
        print(f"Setting environment variable: {key}={val}")
        os.environ[key] = val

    build_command(env, command)
    print(f"The following command will be executed: {command}")
    subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)


if __name__ == "__main__":
    main()
