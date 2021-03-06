import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from typing import Any, List, Optional

from .review import CheckoutOption, Review, nix_shell
from .utils import sh


def parse_pr_numbers(number_args: List[str]) -> List[int]:
    prs: List[int] = []
    for arg in number_args:
        m = re.match(r"(\d+)-(\d+)", arg)
        if m:
            prs.extend(range(int(m.group(1)), int(m.group(2))))
        else:
            try:
                prs.append(int(arg))
            except ValueError:
                print(f"expected number, got {m}", file=sys.stderr)
                sys.exit(1)
    return prs


def pr_command(args: argparse.Namespace) -> None:
    prs = parse_pr_numbers(args.number)
    use_ofborg_eval = args.eval == "ofborg"
    checkout_option = (
        CheckoutOption.MERGE if args.checkout == "merge" else CheckoutOption.COMMIT
    )

    attrsets = []

    with ExitStack() as stack:
        for pr in prs:
            worktree_dir = stack.enter_context(Worktree(f"pr-{pr}"))
            try:
                r = Review(
                    worktree_dir,
                    args.build_args,
                    args.token,
                    use_ofborg_eval,
                    checkout_option,
                )
                attrsets.append(r.build_pr(pr))
            except subprocess.CalledProcessError:
                print(
                    f"https://github.com/NixOS/nixpkgs/pull/{pr} failed to build",
                    file=sys.stderr,
                )

        for attrs in attrsets:
            print(f"https://github.com/NixOS/nixpkgs/pull/{pr}")
            nix_shell(attrs)

        if len(attrsets) != len(prs):
            sys.exit(1)


def rev_command(args: argparse.Namespace) -> None:
    with Worktree(f"rev-{args.commit}") as worktree_dir:
        r = Review(worktree_dir, args.build_args)
        r.review_commit(args.branch, args.commit)


def parse_args(command: str, args: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=command, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--build-args", default="", help="arguments passed to nix when building"
    )
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="subcommands",
        description="valid subcommands",
        help="use --help on the additional subcommands",
    )
    subparsers.required = True  # type: ignore

    pr_parser = subparsers.add_parser("pr", help="review a pull request on nixpkgs")
    pr_parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_OAUTH_TOKEN", None),
        help="Github access token (optional if request limit exceeds)",
    )
    pr_parser.add_argument(
        "--eval",
        default="ofborg",
        choices=["ofborg", "local"],
        help="whether to use ofborg's evaluation result",
    )

    checkout_help = (
        "What to source checkout when building: "
        + "`merge` will merge the pull request into the target branch, "
        + "while `commit` will checkout pull request as the user has committed it"
    )

    pr_parser.add_argument(
        "--checkout", default="merge", choices=["merge", "commit"], help=checkout_help
    )
    pr_parser.add_argument(
        "number",
        nargs="+",
        help="one or more nixpkgs pull request numbers (ranges are also supported)",
    )
    pr_parser.set_defaults(func=pr_command)

    rev_parser = subparsers.add_parser(
        "rev", help="review a change in the local pull request repository"
    )
    rev_parser.add_argument(
        "--branch", default="master", help="branch to compare against with"
    )
    rev_parser.add_argument(
        "commit", help="commit/tag/ref/branch in your local git repository"
    )
    rev_parser.set_defaults(func=rev_command)

    return parser.parse_args(args)


def die(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def find_nixpkgs_root() -> Optional[str]:
    prefix = ["."]
    release_nix = ["nixos", "release.nix"]
    while True:
        root_path = os.path.join(*prefix)
        release_nix_path = os.path.join(root_path, *release_nix)
        if os.path.exists(release_nix_path):
            return root_path
        if os.path.abspath(root_path) == "/":
            return None
        prefix.append("..")


class DisableKeyboardInterrupt:
    def __enter__(self) -> None:
        self.signal_received = False

        def handler(_sig: Any, _frame: Any) -> None:
            print("Ignore Ctlr-C: Cleanup in progress... Don't be so impatient human!")

        self.old_handler = signal.signal(signal.SIGINT, handler)

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        signal.signal(signal.SIGINT, self.old_handler)


class Worktree:
    def __init__(self, name: str) -> None:
        worktree_dir = os.path.join("./.review", name)
        try:
            os.makedirs(worktree_dir)
        except FileExistsError:
            print(
                f"{worktree_dir} already exists. Is a different review already running?"
            )
            raise
        self.worktree_dir: Optional[str] = worktree_dir
        self.nixpkgs_config = tempfile.NamedTemporaryFile()
        self.nixpkgs_config.write(b"pkgs: { allowUnfree = true; }")
        self.nixpkgs_config.flush()

        self.environ = os.environ.copy()
        os.environ["NIXPKGS_CONFIG"] = self.nixpkgs_config.name
        os.environ["NIX_PATH"] = f"nixpkgs={os.path.realpath(worktree_dir)}"
        os.environ["GIT_AUTHOR_NAME"] = "nix-review"
        os.environ["GIT_AUTHOR_EMAIL"] = "nix-review@example.com"
        os.environ["GIT_COMMITTER_NAME"] = "nix-review"
        os.environ["GIT_COMMITTER_EMAIL"] = "nix-review@example.com"

    def __enter__(self) -> str:
        assert self.worktree_dir is not None
        return self.worktree_dir

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.nixpkgs_config is not None:
            self.nixpkgs_config.close()

        if self.environ is not None:
            os.environ.update(self.environ)

        if self.worktree_dir is None:
            return

        with DisableKeyboardInterrupt():
            shutil.rmtree(self.worktree_dir)
            sh(["git", "worktree", "prune"])
            os.environ.clear()


def main(command: str, raw_args: List[str]) -> None:
    root = find_nixpkgs_root()
    if root is None:
        die("Has to be execute from nixpkgs repository")
    else:
        os.chdir(root)

    args = parse_args(command, raw_args)
    args.func(args)
