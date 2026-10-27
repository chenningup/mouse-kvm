# mouse-kvm

一套键鼠，控制多台电脑。Windows 和 Linux 可以混着来。
纯 Python 标准库实现，Windows 端零第三方依赖。

跟 Synergy / Barrier 是一类东西，但这个是自己写的，逻辑简单、能看懂、能改。

## 它是怎么工作的

```
   主机（接着键盘鼠标的那台）
   ┌──────────────────────────────┐
   │  底层钩子捕获键鼠事件          │
   │        ↓                     │
   │  光标推到屏幕边缘 ──→ 切换     │
   │        ↓                     │
   │  TCP :24800  ────────────────┼──→ 从机 A
   │        ↑                     │        从机 B
   └────────┼─────────────────────┘        ...
            │                                ↓
   127.0.0.1:24801                    注入本机键鼠
   （kvm.py send / status 连这里）
```

键鼠事件、剪贴板、文件传输**共用这一条 TCP 连接**，
帧之间靠长度前缀区分，不会互相串。

屏幕是按方位摆的：

```
        [ top ]
  [left]  HOST  [right]
       [ bottom ]
```

光标推到右边并继续往外推，就跳到右侧的从机；在从机上往回推，就回到主机。
最多挂 4 台（四边各一台）。

## 平台支持

| 角色 | Windows | Linux |
|---|---|---|
| 主机（接键鼠） | 支持 | 支持（需 evdev 权限） |
| 从机（被控制） | 支持 | 支持（需 uinput 权限） |

Linux 走的是 evdev/uinput，不依赖 X11，**Wayland 下同样能用**。

## 快速开始

### 1. 主机（接着键盘鼠标的那台）

```bash
python kvm.py host --name winbox --peer ubuntu=right
```

`--peer ubuntu=right` 的意思是：有个叫 `ubuntu` 的从机摆在右边。
挂多台就这样：

```bash
python kvm.py host --name winbox --peer ubuntu=right --peer mac-mini=left
```

### 2. 从机

```bash
python kvm.py client --name ubuntu --host 192.168.1.20
```

`--name` 必须和主机 `--peer` 里写的一致，`--host` 填主机 IP。

### 3. 用起来

鼠标往右推过主机屏幕右边缘 → 光标出现在 Linux 那台上 → 键盘也跟着过去。
在 Linux 上把光标推回左边缘 → 回 Windows。

**抢回控制权**：任何时候按 `Ctrl + Alt + Esc`，立刻切回本机，
这个组合键不会被转发出去。万一从机没响应、键鼠像被卡住，就按它。

## 剪贴板共享

复制即同步，双向。Windows 上 `Ctrl+C`，切到 Linux 直接 `Ctrl+V`，反过来也一样。

### 文本

- **只同步纯文本**。图片、富文本格式一律跳过。
- 单条上限 256KB，超了不同步 —— 大段内容会把网络和注入都拖慢。
- **防回环**：程序自己写进剪贴板的内容不会再被推回去，不会两边互相刷。
- 关掉就用 `--no-clip`。
- 剪贴板同步跟光标在不在那台机器上无关，复制完随时能粘。

### 文件和文件夹

**在 Linux 上 `Ctrl+C` 文件或文件夹，到 Windows 的资源管理器里右键就有「粘贴」。**

不是把路径当文本发过去 —— 那样 Windows 只会收到一串 `/home/xxx/a.zip` 文字，
右键菜单里根本不会出现「粘贴」，因为资源管理器认的是 `CF_HDROP`（真正的文件列表）。
所以做的是真传输：

```
Linux 上 Ctrl+C（一个文件 / 一个文件夹 / 多选）
   ↓ 读到 text/uri-list，发现是文件或目录
文件：直接推
文件夹：先宣告批次拿到对端目录，再按相对路径逐个推（目录结构原样保留）
   ↓ 落到 下载/mouse-kvm/
把落盘路径塞进 Windows 剪贴板当 CF_HDROP
   ↓
右键粘贴 / Ctrl+V 都正常，粘出来的是真文件/真目录
```

以文件夹为例，实测日志：

```
<< 开始接收目录 项目资料（3 个文件）来自 linux
<< 文件已保存 ...\mouse-kvm\项目资料\子目录\a.txt
<< 文件已保存 ...\mouse-kvm\项目资料\子目录\data.bin
<< 文件已保存 ...\mouse-kvm\项目资料\说明.txt
剪贴板已放入 1 个文件，可直接粘贴
```

限制：

- 一次最多同步 20 个顶层项；单文件上限 512MB，文件夹最多 2000 个文件、合计 2GB。
- 空目录传不了（没有文件可传，剪贴板里也不会出现它）。带内容的空子目录同样会丢。
- 符号链接会跳过。
- 剪贴板里落的是 `下载/mouse-kvm/` 下的副本，粘贴到别处是再复制一份。
  原文件在 Linux 上，那边不动。
- **只有 Linux → Windows 这个方向**。反方向（Windows 复制文件 → Linux 粘贴）
  需要 GNOME 的 `x-special/gnome-copied-files`，各文件管理器兼容性不一，
  暂时没做 —— 用 `kvm.py send` 推过去即可。
- 文件名/目录名会自动加后缀避免覆盖（`a.txt` → `a-1.txt`，`项目资料` → `项目资料-1`）。

Linux 端需要剪贴板工具，Wayland 装 `wl-clipboard`，X11 装 `xclip`：

```bash
sudo apt install wl-clipboard     # Wayland（Ubuntu 22.04 默认）
sudo apt install xclip            # X11 会话
```

程序启动时会打印用的是哪个实现，例如 `剪贴板同步已启用（wayland）`。
如果提示不可用，说明上面两个都没装，或者当前会话没有图形环境变量。

## 文件传输

一条命令把文件推给对端，**双向都行**，落到对方的 `下载/mouse-kvm/` 目录。

```bash
# Windows 上推给 Linux
python kvm.py send 报告.pdf

# Linux 上推给 Windows（在从机那边执行同样命令）
python3 kvm.py send ~/a.zip

# 整个文件夹，目录结构原样保留
python kvm.py send ./项目资料

# 多台从机时指定目标
python kvm.py send a.zip --to ubuntu

# 一次传多个
python kvm.py send a.txt b.txt c.zip
```

跑起来长这样：

```
  -> linux  1.4 MB
  100.0%  1.4 MB / 1.4 MB
  已送达 linux：/home/inper/Downloads/mouse-kvm/report.pdf
```

要用的时候两端的 KVM 必须在运行 —— `send` 只是给正在跑的进程发个指令，
自己不建连接、也不做传输。看一眼谁在线：

```bash
python kvm.py status
```

```
主机：winbox
  linux            位置=right  屏幕=3840x2160  在线
```

细节：

- 走的是 KVM **已有的那条 TCP 连接**，不额外开端口，不用配 SCP/SFTP/共享目录。
- 重名自动加后缀（`a.txt` → `a-1.txt`），**绝不覆盖**已有文件。
  文件夹是整体判断的：`项目资料` 已存在就整个落到 `项目资料-1`，不会把文件混着塞进旧目录。
- 接收方先写 `.part` 临时文件，大小核对上了才改成正式名字。
  传到一半断了不会留下半截文件，也不会让你以为收到了。
- 单文件上限 512MB；文件夹最多 2000 个文件、合计 2GB。
- 文件名为目录结构里的每一级都做了清洗（路径穿越、非法字符、Windows 保留名）。
  符号链接会跳过，避免成环或跳出目录。
- 传输过程不打断键鼠操作。发送在独立线程里跑，两边共用连接但帧不会串。
- 中文文件名和中文目录名正常。

速度取决于网络。这台机器实测 100MB 用了 29 秒（约 3.5 MB/s），
同一条链路上 `scp` 是 28 秒 —— 也就是说瓶颈在带宽，不在协议。

浏览器下载那种"复制文件"的操作不会自动触发传输，得显式跑 `send`。

## 常驻运行

Linux 从机已经配好 systemd 用户服务，开机自启、挂了自动重拉：

```bash
systemctl --user status  mouse-kvm-client    # 看状态
systemctl --user restart mouse-kvm-client    # 重启
systemctl --user disable --now mouse-kvm-client  # 停用
tail -f /tmp/kvm-client.log                  # 看日志
```

配置文件在 `~/.config/systemd/user/mouse-kvm-client.service`，
换主机 IP 就改里面的 `--host`，然后 `systemctl --user daemon-reload && systemctl --user restart mouse-kvm-client`。

Windows 主机双击 `run-host.bat` 即可，窗口别关。想开机自启：
按 `Win+R` 输入 `shell:startup`，把 `run-host.bat` 的快捷方式丢进去。

## Linux 权限配置

Linux 要读 `/dev/input/event*` 和写 `/dev/uinput`，默认只有 root 能干。
不想每次 `sudo` 就配一下（**改完要重新登录**）：

```bash
sudo usermod -aG input $USER

sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf

sudo cp 99-mouse-kvm.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

配完验证一下，动动鼠标键盘应该能看到事件刷出来：

```bash
python3 kvm.py probe
```

没输出就是权限没生效，先用 `sudo` 跑确认是权限问题。

Linux 端还需要装一个包：

```bash
pip install evdev
```

## 网络

- 默认端口 `24800`，主机上要放行（Windows 首次运行会弹防火墙提示，点允许）。
- 只想局域网用，别往公网暴露。协议是明文的，没有加密。
- 两台机器要能互相 ping 通。

想加个口令防止别人乱连：

```bash
# 两台都加同一个 --token
python kvm.py host   --name winbox --peer ubuntu=right --token mysecret
python kvm.py client --name ubuntu --host 192.168.1.20 --token mysecret
```

## 常用参数

主机：

| 参数 | 说明 |
|---|---|
| `--name` | 本机名字，只用于显示 |
| `--port` | 监听端口，默认 24800 |
| `--peer NAME=SIDE` | 从机名和方位，`SIDE` 取 `left/right/top/bottom`，可重复 |
| `--token` | 连接口令 |
| `--edge N` | 推到边缘多少次触发切换，默认 3，越小越灵敏 |
| `--no-clip` | 关闭剪贴板同步 |
| `--bind` | 监听地址，默认 `0.0.0.0` |
| `--ctl-port` | 本地控制通道端口，`send`/`status` 用，默认 `端口+1` |

从机：

| 参数 | 说明 |
|---|---|
| `--name` | 必须和主机 `--peer` 里的名字一致 |
| `--host` | 主机 IP |
| `--port` | 主机端口 |
| `--token` | 连接口令 |
| `--no-clip` | 关闭剪贴板同步 |
| `--ctl-port` | 本地控制通道端口，默认 `端口+1` |

`send` / `status`：

| 参数 | 说明 |
|---|---|
| `--to` | 目标从机名，只有一台时可省略 |
| `--ctl-port` | 要连的控制通道端口，默认 24801 |
| `--ctl-host` | 控制通道地址，默认 `127.0.0.1` |

## 自测

```bash
python kvm.py probe              # 只监听不拦截，验证钩子/权限
python tests/test_logic.py       # 主机端状态机（假后端，不碰真实输入）
python tests/test_client.py      # 从机端注入与按键清理
python tests/test_clip.py        # 剪贴板同步、去重、防回环
python tests/test_filetransfer.py # 文件分块、拼回、大小校验、完整往返
python tests/test_inject.py      # 真实注入（会短暂动一下光标，自动归位）
python tools/diag_clip.py        # 看 Windows 剪贴板里有哪些格式（排查粘贴问题）
```

## 出问题时先看这里

**光标推到边缘不切换**
主机终端里应该打印 `>> 切换到 xxx`。没打印说明边缘判定没触发，把 `--edge` 调小到 2 试试。
Windows 上如果光标停在边缘不动，系统不会产生新事件，需要继续推一下。

**切过去之后键盘没反应 / 主机还在响应**
Windows 主机的键盘压制依赖低级钩子，某些安全软件会拦。看主机日志有没有报错。
Linux 主机需要 `grab` 设备，权限不够会静默失败 —— 用 `probe` 确认能读到事件。

**从机连不上**
先 `ping` 通，再查防火墙端口。从机日志会打印 `连接中断` 的原因。

**Linux 从机光标乱跳 / 位置不准**
绝对坐标注入在部分桌面环境下不被识别。试试相对模式：后端构造时传 `use_abs=False`，
或者在代码里改 `LinuxBackend(use_abs=False)`。相对模式会受系统鼠标加速影响。

**回到本机后 Ctrl 卡住**
正常不会。真遇到了说明切换时按键释放没执行，把 `tests/test_client.py` 跑一遍看是哪一步漏了。

**Linux 上复制文件，Windows 右键没有"粘贴"**
先跑 `python tools/diag_clip.py` 看 Windows 剪贴板里是什么格式：

- 只有 `CF_UNICODETEXT`，内容是 `/home/xxx/a.zip` 这样的路径文字
  → 那是把文件当文本发了，资源管理器不认。确认从机是当前版本
  （日志里应该有 `剪贴板里有 1 个文件，开始推到主机`）。
- 有 `CF_HDROP` → 剪贴板已经就绪，右键"粘贴"应该正常。
  如果还是没有，是那个程序自己的问题（比如在某个应用的输入框里右键，
  文本粘贴走的是 `CF_UNICODETEXT`，跟文件是两条路）。

**`send` 报"连不上控制通道"**
两端进程没在跑，或者 `--port` 改过但 `send` 没跟着改 `--ctl-port`。
`kvm.py status` 能查在线情况；控制通道只监听 `127.0.0.1`，不能跨机用。

**`send` 报"找不到从机"**
从机还没连上来，或者名字跟主机 `--peer` 里写的不一致。`status` 会列出在线的名字。

**文件传到一半断了**
对端会收到 `大小不符` 并删掉临时文件，主机这边 `send` 直接报错退出。
重跑一次即可，重名的会自动变成 `-1`。

## 已知限制

- 从机只支持单显示器，多屏会全部映射到主屏。
- 剪贴板同步文本是双向的；复制文件/文件夹只支持 Linux → Windows（`CF_HDROP`），
  反方向没做。文本单条上限 256KB。
- 文件传输要显式跑 `send`，没有做拖拽触发；单文件上限 512MB。
- 空目录不会传输（只传文件，不传目录本身）。
- 没做音频传输。
- 不支持 macOS（后端没写）。
- Linux 做主机的时，光标坐标是自己累加估算的（Wayland 没有查询光标的公开接口），
  在屏幕边缘会自动对齐，中间位置可能有一两个像素误差，不影响使用。
- 从机必须和主机同一个网段，跨公网延迟会让手感变差。

## 实测记录

跑通的环境（Windows 主机 + Linux 从机）：

| 项目 | 情况 |
|---|---|
| 主机 | Windows 11，3840x2160 @150% 缩放 |
| 从机 | Ubuntu 22.04，Wayland 会话（XWayland），3840x2160 |
| 网络 | 同网段无线，往返约 100ms |
| 结果 | 边缘切换、键鼠转发、左右来回切换全部正常 |

几个实测结论：

- Linux 从机的绝对坐标注入**精确命中**，注 1920 就是 1920，没有漂移。
- Wayland 下 uinput 虚拟鼠标能被正常识别成指针设备（`xinput` 里能看到 slave pointer）。
- 相对模式注入会被系统鼠标加速放大，所以默认走绝对坐标。
- 两台都是 4K 时坐标是 1:1 映射；分辨率不同会按比例缩放。
- 剪贴板双向实测：`WIN2LINUX_MARKER` / `LINUX2WIN_MARKER` 两个方向都到达。
- 文件传输双向实测 MD5 一致：1.5MB 文本、900KB 二进制、100MB 随机数据各一遍。
- 100MB 传输 29 秒 vs `scp` 28 秒，协议开销可以忽略，瓶颈在带宽。
- Linux 复制文件 → Windows 剪贴板里出现 `CF_HDROP`，文件落在
  `下载/mouse-kvm/`，右键粘贴拿到的是真文件。
- Linux 复制**文件夹** → Windows 落地成完整目录树（`项目资料\子目录\a.txt` 等），
  剪贴板里放的是那个目录本身，右键粘贴整个目录一起走。
- `kvm.py send 目录` 反方向也验过，目录结构一致。


## 目录结构

```
kvm.py                     命令行入口（host / client / send / status / probe）
mouse_kvm/
  protocol.py              长度前缀 + JSON 的 TCP 帧
  keys.py                  Windows VK <-> Linux evdev 键码双向映射
  host.py                  主机状态机：边缘判定、坐标映射、压制转发
  client.py                从机：接收并注入
  clipboard.py             剪贴板同步（Win32 API / wl-clipboard / xclip）
  filetransfer.py          文件分块收发（两端共用，base64 塞 JSON 帧）
  control.py               本地控制通道（127.0.0.1，一行一个 JSON）
  backend/
    base.py                后端抽象
    win32.py               Win32 低级钩子 + SendInput
    linux.py               evdev + uinput
tests/                     自测脚本
99-mouse-kvm.rules         Linux udev 规则
```

## 实现上几个值得说的点

**为什么不用 pynput**：它的 `suppress` 只能在启动时设定，没法运行时切换，
而 KVM 必须在"本机"和"转发"两种状态间来回切。所以 Windows 端直接写 Win32 钩子。

**Windows 上光标怎么锁住**：远程模式下每次收到鼠标移动，就把光标 `SetCursorPos`
拉回锚点，用两点的差值当作位移量。低级钩子拦不住光标移动本身，只能这么绕。

**为什么从机不做边缘判断**：坐标全在主机侧算，从机是纯执行端。
这样两端不会因为浮点误差或事件丢失产生状态分歧。

**为什么要做 DPI 感知**：4K 屏开 150% 缩放时，不设 DPI 感知会让
`GetSystemMetrics` 和钩子返回的坐标不是一个坐标系，注入位置就会偏。

**文件为什么走 base64 而不是二进制帧**：协议是"长度前缀 + JSON"，
塞二进制就得改帧格式、加分流逻辑。base64 多 33% 体积，但在这条链路上
带宽才是瓶颈（实测跟 `scp` 打平），省下的复杂度更值。分块 256KB 是因为
帧上限 8MB，块太大一旦出错要重传的也多。

**为什么 `send` 要靠本地控制通道**：`kvm.py send` 是个短命进程，
它拿不到主机进程里那些连接对象。开一条 `127.0.0.1` 的 socket，
`send` 把"文件路径 + 目标"发过去，真正的传输由常驻进程完成 ——
这样 CLI 退出也不影响传输，还能顺便把进度一行行回传。

**传输为什么不阻塞键鼠**：发送在独立线程里跑，每帧发送前拿
`FramedConnection` 的锁。所以大文件传到一半你照样能把鼠标推过去，
最多多几毫秒延迟。
