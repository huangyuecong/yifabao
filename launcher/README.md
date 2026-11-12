# 独立启动器

重新编译 EXE：

```powershell
Set-Location "<项目>/launcher"
.\build.ps1
```

唯一的日常启动入口位于项目根目录：`AI个性化内容智能体启动器.exe`。编译不会再生成 `launcher/ContentLauncher.exe`。

本目录中的 `start.ps1`、`process-utils.ps1`、`check-frontend.mjs` 和停止脚本是 EXE 的内部依赖，必须保留；`ContentLauncher.cs` 和 `build.ps1` 是源码和构建工具。

`ScriptRunner.cs` 负责脚本进程、取消和超时控制，构建时一并编译。单次服务操作最长等待 10 分钟；脚本退出后最多等待 2 秒收齐日志，避免子进程继承输出管道导致窗口一直显示启动中。取消启动后可以直接重试。

回归检查：`powershell -NoProfile -ExecutionPolicy Bypass -File launcher/tests/launcher.test.ps1`。

维护检查（不启动或停止服务）：`powershell -NoProfile -ExecutionPolicy Bypass -File launcher/start.ps1 -CheckOnly`，从项目根目录执行。

缺失依赖时启动检查会失败，且不自动执行安装。按根目录 README 中的两步命令安装前端依赖，然后重新检查。依赖从公共 npm 仓库安装，不需要公司私有组件仓库。不要从其他项目复制依赖，也不要删除锁文件。

“停止服务”同时停止本项目的前后端。停止前核对命令行、项目目录及记录的进程启动时间；不会按端口直接强杀其他项目的进程。日志位于根目录 `launcher-logs` 和 `backend/work/logs`。

启动顺序：`8002 → 8200`。不会启动会议、声纹或说话人分离服务。

当前本机工作台采用免登录模式，前后端均监听 `127.0.0.1`；后端关闭代理身份转发解析。模型配置可直接从“模型与服务”编辑。修改运行模式后先停止再启动，避免复用仍加载旧配置的服务进程。

## 服务器安卓屏幕

双击本目录的 `打开服务器安卓屏幕.cmd`，通过 SSH 隧道打开服务器上的安卓屏幕。关闭安卓窗口会清理本次启动的隧道和独立 ADB 服务。服务器 ADB 只绑定回环地址，不需要在安全组开放 ADB 端口。

入口使用 `tools/scrcpy/scrcpy.exe` 及同目录的官方 `adb.exe`，需要 Windows OpenSSH 客户端和已有的可信服务器主机记录。服务器地址和私钥路径通过 `-ServerHost`、`-KeyPath` 参数，`ANDROID_SERVER_HOST`、`ANDROID_SERVER_SSH_KEY` 环境变量，或不提交到仓库的 `launcher/open-server-android.local.psd1`（`@{ ServerHost = '...'; KeyPath = '...' }`）提供；脚本不复制私钥，也不自动接受未知主机。

仅检查依赖（不连接服务器、不启动安卓屏幕）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File launcher/open-server-android.ps1 -CheckOnly
```

本机转发端口默认为 `15555`，独立 ADB 服务端口为 `15037`。任一端口已被占用时会停止并说明原因，不会结束现有进程。需要调整时：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File launcher/open-server-android.ps1 -LocalPort 15556 -AdbServerPort 15038
```

可通过 `-ServerHost`、`-ServerUser`、`-KeyPath`、`-ServerAdbPort` 和 `-ScrcpyDirectory` 覆盖连接信息。运行日志保存在项目根目录的 `launcher-logs/server-android-*.log`。这个入口用于查看、操作安卓系统；项目自动发布适配需另外配置。

2026-10-18 已完成真实连接验收：服务器 Android 15 启动完成，SSH 隧道和 ADB 状态正常；scrcpy 在不播放、不操作屏幕的模式下录制成功，生成的 H.264 视频经完整解码得到 11 帧有效的 720×1280 安卓桌面画面。入口默认限制 15 FPS、2 Mbps，适配当前服务器；未执行 App 登录或发布操作。服务器使用 `default` gralloc 及软件 Codec2 配置，维护时需保留这些配置，否则可能出现没有编码器或缓冲分配失败。
