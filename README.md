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
   └──────────────────────────────┘        从机 B
                                          ...
                                            ↓
                                     注入本机键鼠
```

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

- **只同步文本**。图片、文件、富文本格式一律跳过。
- 单条上限 256KB，超了不同步 —— 大段内容会把网络和注入都拖慢。
- **防回环**：程序自己写进剪贴板的内容不会再被推回去，不会两边互相刷。
- 关掉就用 `--no-clip`。
- 剪贴板同步跟光标在不在那台机器上无关，复制完随时能粘。

Linux 端需要剪贴板工具，Wayland 装 `wl-clipboard`，X11 装 `xclip`：

```bash
sudo apt install wl-clipboard     # Wayland（Ubuntu 22.04 默认）
sudo apt install xclip            # X11 会话
```

程序启动时会打印用的是哪个实现，例如 `剪贴板同步已启用（wayland）`。
如果提示不可用，说明上面两个都没装，或者当前会话没有图形环境变量。

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

从机：

| 参数 | 说明 |
|---|---|
| `--name` | 必须和主机 `--peer` 里的名字一致 |
| `--host` | 主机 IP |
| `--port` | 主机端口 |
| `--token` | 连接口令 |
| `--no-clip` | 关闭剪贴板同步 |

## 自测

```bash
python kvm.py probe              # 只监听不拦截，验证钩子/权限
python tests/test_logic.py       # 主机端状态机（假后端，不碰真实输入）
python tests/test_client.py      # 从机端注入与按键清理
python tests/test_inject.py      # 真实注入（会短暂动一下光标，自动归位）
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

## 已知限制

- 从机只支持单显示器，多屏会全部映射到主屏。
- 剪贴板只同步纯文本，图片、文件、富文本不同步。单条上限 256KB。
- 没做文件拖拽和音频传输。
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


## 目录结构

```
kvm.py                     命令行入口（host / client / probe）
mouse_kvm/
  protocol.py              长度前缀 + JSON 的 TCP 帧
  keys.py                  Windows VK <-> Linux evdev 键码双向映射
  host.py                  主机状态机：边缘判定、坐标映射、压制转发
  client.py                从机：接收并注入
  clipboard.py             剪贴板同步（Win32 API / wl-clipboard / xclip）
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
