# Windows 11 setup with WSL2

AgileCode currently targets Linux. On Windows 11, the Linux application can run inside Ubuntu using Windows Subsystem for Linux 2 (WSL2). WSLg displays the PySide6 desktop interface as a window on the Windows desktop. The execution engine, Git operations, shell commands, and ticket worktrees remain inside Linux.

These instructions use Ubuntu 24.04 and Python 3.13. This is a WSL installation of the Linux release, not native Windows support.

## 1. Install WSL2 and Ubuntu

Open **PowerShell as Administrator** in Windows and run:

~~~powershell
wsl --install -d Ubuntu-24.04
~~~

Restart Windows if prompted. Open Ubuntu 24.04 and create a Linux username and password when prompted.

From PowerShell, confirm that the distribution uses WSL version 2:

~~~powershell
wsl --list --verbose
~~~

Windows 11 includes WSLg support for Linux graphical applications. An additional X server or Linux desktop environment is not required.

If WSL was already installed but graphical applications do not open, update WSL from PowerShell and restart it:

~~~powershell
wsl --update
wsl --shutdown
~~~

## 2. Install dependencies in Ubuntu

Run these commands in the **Ubuntu terminal**, not in PowerShell:

~~~bash
sudo apt update
sudo apt install -y git pipx software-properties-common curl libxcb-cursor0 libxkbcommon-x11-0
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.13 python3.13-venv
python3.13 --version
pipx ensurepath
~~~

Ubuntu 24.04's default Python is 3.12, while AgileCode requires Python 3.13 or newer. The commands above install Python 3.13 alongside the system interpreter; do not replace Ubuntu's system Python.

Close and reopen the Ubuntu terminal after `pipx ensurepath` so its application directory is available on `PATH`.

## 3. Install AgileCode

Keep the application and the projects it manages in the WSL Linux filesystem:

~~~bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/JakeChrist/AgileCode.git
cd AgileCode
pipx install --force --python python3.13 '.[desktop]'
~~~

The pipx installation makes `agilecode` and `agilecode-gui` available without manually activating a virtual environment. To install updated source after pulling new commits, rerun the pipx installation command from the AgileCode repository directory.

## 4. Prepare a Git project and launch the GUI

The argument to `agilecode-gui` is the **project to manage**, not necessarily the AgileCode source directory. The target project must be an initialized Git repository containing at least one commit and with a configured Git author identity.

For example, if the project already has a Git remote:

~~~bash
cd ~/projects
git clone YOUR_PROJECT_REPOSITORY_URL YourProject
agilecode-gui ~/projects/YourProject
~~~

If you have not configured a Git author identity inside Ubuntu, configure it there; Windows Git settings are not automatically shared:

~~~bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
~~~

With WSLg working, the AgileCode GUI appears as a normal window on the Windows desktop. Its processes and development tools still execute inside Ubuntu.

## 5. Where to store projects

Prefer paths such as:

~~~text
/home/<linux-user>/projects/AgileCode
/home/<linux-user>/projects/YourProject
~~~

Avoid placing working Git repositories under `/mnt/c/Users/...` when running AgileCode in WSL. The Linux filesystem generally performs better for Linux development tools and Git worktrees and avoids cross-filesystem permission and metadata differences.

Windows File Explorer can open the same files at:

~~~text
\\wsl$\Ubuntu-24.04\home\<linux-user>\projects
~~~

Replace `<linux-user>` with the Ubuntu username you created.

## 6. Connecting to LM Studio on Windows

LM Studio may run directly on Windows while AgileCode runs in Ubuntu. Start the LM Studio API server and load the model you intend to use.

### Recommended: mirrored WSL networking

By default, WSL2 often uses NAT networking: `127.0.0.1` inside Ubuntu does not address a service bound to Windows localhost. Windows 11 supports *mirrored networking*, which allows WSL applications to access Windows services using `127.0.0.1`.

In **Windows PowerShell**, open your Windows user WSL configuration file:

~~~powershell
notepad "$env:USERPROFILE\.wslconfig"
~~~

Add the following section (or add the setting to an existing `[wsl2]` section):

~~~ini
[wsl2]
networkingMode=mirrored
~~~

Save the file, then run in PowerShell:

~~~powershell
wsl --shutdown
~~~

Open Ubuntu again. With the LM Studio server running on Windows, test its endpoint from Ubuntu:

~~~bash
curl http://127.0.0.1:1234/v1/models
~~~

If it responds, configure the AgileCode provider's base URL as:

~~~text
http://127.0.0.1:1234/v1
~~~

The model identifier must match what LM Studio exposes. If the server uses a different port or requires authentication, use its actual configuration.

### Alternative: default NAT networking

When using the default WSL2 NAT networking mode, obtain the Windows host IP from Ubuntu:

~~~bash
ip route show default
~~~

The IP following `via` is normally the Windows host's address as seen from WSL. Use `http://<windows-host-ip>:1234/v1` as the provider URL. In this mode, the Windows API server must accept connections on the host network interface, not just `127.0.0.1`, and the Windows firewall must permit the connection. In LM Studio, this generally means enabling its option to serve on the local network.

## Limitations and troubleshooting

- **This is Linux execution on Windows.** Tickets run Bash and Linux development tools. Windows-specific build tools, PowerShell workflows, and native Windows application testing are not automatically provided by WSL.
- **No GUI window appears:** Verify that the distribution is WSL2 (`wsl --list --verbose`), update WSL (`wsl --update`), and restart it (`wsl --shutdown`). WSLg requires a compatible Windows and WSL installation.
- **Qt complains about the `xcb` platform plugin:** Verify that `libxcb-cursor0` and `libxkbcommon-x11-0` are installed in Ubuntu.
- **LM Studio connection fails:** First test the `/v1/models` endpoint with `curl` inside Ubuntu. Check whether WSL is using mirrored or NAT networking, whether LM Studio's server is running, and whether the correct port is configured.
- **Git identity errors:** Configure `user.name` and `user.email` inside Ubuntu, and ensure the project has an initial commit.
- **Concurrent hosts:** Only one AgileCode host should have writable ownership of a given project's execution engine at a time.

For broader application usage, see [USER_MANUAL.md](USER_MANUAL.md).

### References

- [Microsoft: Install WSL](https://learn.microsoft.com/en-us/windows/wsl/install)
- [Microsoft: Run Linux GUI apps with WSL](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps)
- [Microsoft: WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [Microsoft: WSL configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)
