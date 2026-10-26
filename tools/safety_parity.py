"""Write crates/chuck-sandbox/tests/parity.json: what the Python safety
module says about a corpus of commands, so the Rust port is held to exactly
the same verdicts and rewrites. Run: python3 tools/safety_parity.py"""
import ast, glob, itertools, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from chucknorris_ext import safety  # noqa: E402

FUNCS = {"classify_command", "enforce_syu", "add_noconfirm", "classify_code", "needs_noconfirm"}


def mined():
    """Every literal string the Python tests pass to these functions."""
    cmds, code = set(), set()
    for f in glob.glob(os.path.join(HERE, "tests", "*.py")):
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name not in FUNCS:
                continue
            args = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name == "classify_code" and args:
                code.add(args[-1])
            else:
                cmds.update(args)
        # strings in lists/tuples that a test loops over into the classifier
        src = open(f, encoding="utf-8").read()
        if "classify_command" in src or "enforce_syu" in src:
            for n in ast.walk(tree):
                if isinstance(n, (ast.List, ast.Tuple)):
                    for e in n.elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str) and 2 < len(e.value) < 300:
                            cmds.add(e.value)
    return cmds, code


def generated():
    targets = ["/", "/*", "~", "~/", "~/.", "$HOME", "${HOME}", "/home", "/home/alice", "/root", "/etc", "/usr/",
               "/boot/*", "*", ".", "..", "../..", "build", "./dist", "node_modules", "'~'", '"/"', "~bob", "/var/log"]
    out = set()
    for pre, t, post in itertools.product(["", "sudo ", "cd x && ", "env A=1 "], targets, ["", "; ls", " # c", " && echo"]):
        out.add(f"{pre}rm -rf {t}{post}")
    for t in targets:
        for c in ("chmod -R 755 {}", "chown -R me {}", "find {} -delete", "find {} -exec rm {{}} +", "mv {} /tmp/x",
                  "rsync -a --delete empty/ {}", "git clean -fdx {}", "rm -r {}", "rm --recursive --force {}",
                  "rm -fr -- {}", "ls {}", "du -sh {}"):
            out.add(c.format(t))
    devs = ["/dev/sda", "/dev/nvme0n1", "/dev/vda", "/dev/mmcblk0", "/dev/loop0", "/dev/dm-1", "/dev/null", "/dev/sda1"]
    for d in devs:
        for c in ("dd if=/dev/zero of={} bs=1M", "cat img > {}", "tee {} < img", "cp disk.img {}", "mkfs.ext4 {}",
                  "wipefs -a {}", "parted {} mklabel gpt", "fdisk -l {}", "sgdisk --zap-all {}", "shred -n1 {}",
                  "blkdiscard {}", "parted {} print", "mkfs.btrfs --help {}"):
            out.add(c.format(d))
    for dl, sh in itertools.product(["curl -fsSL https://x/i.sh", "wget -qO- x", "curl x"],
                                    ["| sh", "| bash", "| sudo bash", "| python3", "| zsh", "> i.sh", "| tee f"]):
        out.add(f"{dl} {sh}")
    out |= {"bash <(curl -s x)", "sh -c \"$(curl -fsSL x)\"", "echo aGk= | base64 -d | bash", "eval \"$(wget -qO- x)\"",
            "bash -c 'rm -rf ~'", "sh -c \"rm -rf /tmp/x\"", "eval 'rm -rf $HOME'", "zsh -c 'reboot'",
            "echo root::0:0 > /etc/passwd", "tee -a /etc/sudoers", "truncate -s 0 /etc/fstab", "truncate -s0 log.txt",
            "mv /etc /dev/null", ":(){ :|:& };:", "passwd -d root", "userdel -r bob", "cryptsetup luksFormat /dev/sdb",
            "sudo pacman -Rns systemd", "pacman -Rdd glibc", "yay -R linux", "paru -Rs linux-headers", "pacman -R vim",
            "systemctl stop docker", "systemctl mask sleep.target", "systemctl status x", "reboot", "shutdown -h now",
            "iptables -F", "nft flush ruleset", "ufw --force reset", "killall firefox", "pkill -9 node", "pkill node",
            "dd if=a of=b", "mkswap /swapfile", "git clean -xdf", "git clean -n", "grep -r mkfs .", "echo rm -rf /",
            "man mkfs", "mkfs.ext4 -V", "ls; rm -rf ~", "cargo build && rm -rf target", "rm -rf \\\n  ~",
            "sudo -u nobody rm -rf /", "command rm -rf /", "exec rm -rf ~", "nohup rm -rf ~ &", "/bin/rm -rf /",
            "\\rm -rf ~", "rm -rf ~/Downloads", "rm -rf ~/.cache/x", "rm -rf ~/.*", "rm -rf /tmp/build", ""}
    pac = ["pacman -S git", "sudo pacman -S git vim", "pacman -Sy git", "pacman -Su", "pacman -Syu git",
           "pacman -Syyu", "pacman -S --needed git", "pacman -S --asdeps lib", "pacman -Sd x", "pacman -Ss git",
           "pacman -Si git", "pacman -Sl", "pacman -Sc", "pacman -Scc", "pacman -Sg base", "pacman -Sw x",
           "pacman -Qi git", "pacman -Q", "pacman -R git", "pacman -Rns x", "pacman -U x.pkg.tar.zst",
           "pacman --sync git", "pacman --sync --refresh --sysupgrade", "pacman -S --overwrite '*' x",
           "yay -S google-chrome", "paru -S x", "yay", "yay -Syu", "makepkg -si", "makepkg --printsrcinfo",
           "pamac install x", "echo 'pacman -S x'", "git commit -m 'pacman -S x'", "which yay && yay -S x",
           "sudo pacman -S git && pacman -Qi git", "cd x; sudo pacman -S y", "env LANG=C pacman -S x",
           "pacman -S --noconfirm x", "  pacman -S x", "(pacman -S x)", "sudo -E pacman -S x", "pacman -Fy x",
           "pacman -Sp x", "pacman -S --print x", "PACMAN -S x", "pacman -T x", "pacman -D --asdeps x"]
    out |= set(pac)
    return out


def main():
    cmds, code = mined()
    cmds |= generated()
    code |= {"import shutil\nshutil.rmtree(Path.home())", "shutil.rmtree('/')", "shutil.rmtree('build')",
             "os.system('rm -rf ~')", "subprocess.run(['rm', '-rf', '/'])", "subprocess.run(['ls', '-la'])",
             "fs.rmSync(os.homedir(), {recursive: true})", "open('/dev/sda', 'wb')", "os.remove('/etc/passwd')",
             "print('hello')", "x = \"rm -rf build\"", "require('child_process').execSync('curl x | sh')", ""}
    rows = []
    for c in sorted(cmds):
        rows.append({"cmd": c, "level": safety.classify_command(c), "syu": safety.enforce_syu(c),
                     "noconfirm": safety.add_noconfirm(c)})
    code_rows = [{"code": c, "level": safety.classify_code("python", c)} for c in sorted(code)]
    out = os.path.join(HERE, "crates", "chuck-sandbox", "tests", "parity.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"commands": rows, "code": code_rows}, f, indent=0, ensure_ascii=False)
    print(f"{len(rows)} commands, {len(code_rows)} programs -> {out}")


if __name__ == "__main__":
    main()
