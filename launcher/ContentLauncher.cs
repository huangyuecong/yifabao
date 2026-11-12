using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace YifabaoContentLauncher
{
    internal static class NativeMethods
    {
        public const int SwRestore = 9;

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);

        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hWnd);
    }

    internal static class Program
    {
        internal const string WindowTitle = "AI个性化内容智能体启动器";

        [STAThread]
        private static void Main()
        {
            bool createdNew;
            using (Mutex mutex = new Mutex(true, @"Local\YifabaoContentAgentLauncher", out createdNew))
            {
                if (!createdNew)
                {
                    IntPtr hwnd = NativeMethods.FindWindow(null, WindowTitle);
                    if (hwnd != IntPtr.Zero)
                    {
                        NativeMethods.ShowWindow(hwnd, NativeMethods.SwRestore);
                        NativeMethods.SetForegroundWindow(hwnd);
                    }
                    else
                    {
                        MessageBox.Show("AI个性化内容智能体启动器已经在运行，请查看现有窗口。", WindowTitle,
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
                    }
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new LauncherForm());
            }
        }
    }

    internal sealed class LauncherForm : Form
    {
        private const string PageUrl = "http://127.0.0.1:8200/content/";
        private readonly object _logLock = new object();
        private Label _statusLabel;
        private Label _detailLabel;
        private ProgressBar _progress;
        private TextBox _logBox;
        private Button _retryButton;
        private Button _openButton;
        private Button _logsButton;
        private Button _stopButton;
        private bool _busy;
        private volatile bool _closing;
        private volatile bool _cancelRequested;
        private string _projectRoot;
        private string _logRoot;

        public LauncherForm()
        {
            Text = Program.WindowTitle;
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(720, 520);
            Size = new Size(820, 600);
            BackColor = Color.FromArgb(246, 249, 248);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular);
            AutoScaleMode = AutoScaleMode.Dpi;

            TableLayoutPanel root = new TableLayoutPanel();
            root.Dock = DockStyle.Fill;
            root.Padding = new Padding(28, 24, 28, 24);
            root.RowCount = 6;
            root.ColumnCount = 1;
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Controls.Add(root);

            Label title = new Label();
            title.AutoSize = true;
            title.Text = "AI个性化内容智能体";
            title.Font = new Font("Microsoft YaHei UI", 22F, FontStyle.Bold);
            title.ForeColor = Color.FromArgb(18, 112, 94);
            title.Margin = new Padding(0, 0, 0, 6);
            root.Controls.Add(title, 0, 0);

            _statusLabel = new Label();
            _statusLabel.AutoSize = true;
            _statusLabel.Text = "准备启动";
            _statusLabel.Font = new Font("Microsoft YaHei UI", 13F, FontStyle.Bold);
            _statusLabel.ForeColor = Color.FromArgb(45, 55, 52);
            root.Controls.Add(_statusLabel, 0, 1);

            _detailLabel = new Label();
            _detailLabel.AutoSize = true;
            _detailLabel.Text = "将启动内容服务和内容前端，不启动会议或声纹服务。";
            _detailLabel.ForeColor = Color.FromArgb(88, 102, 98);
            _detailLabel.Margin = new Padding(0, 8, 0, 8);
            _progress = new ProgressBar();
            _progress.Dock = DockStyle.Top;
            _progress.Height = 8;
            _progress.Style = ProgressBarStyle.Marquee;
            _progress.MarqueeAnimationSpeed = 30;
            TableLayoutPanel progressPanel = new TableLayoutPanel();
            progressPanel.Dock = DockStyle.Top;
            progressPanel.AutoSize = true;
            progressPanel.Controls.Add(_detailLabel, 0, 0);
            progressPanel.Controls.Add(_progress, 0, 1);
            root.Controls.Add(progressPanel, 0, 2);

            _logBox = new TextBox();
            _logBox.Dock = DockStyle.Fill;
            _logBox.Multiline = true;
            _logBox.ReadOnly = true;
            _logBox.ScrollBars = ScrollBars.Vertical;
            _logBox.BackColor = Color.White;
            _logBox.Font = new Font("Consolas", 9.5F);
            root.Controls.Add(_logBox, 0, 3);

            FlowLayoutPanel buttons = new FlowLayoutPanel();
            buttons.AutoSize = true;
            buttons.Margin = new Padding(0, 16, 0, 8);
            _retryButton = MakeButton("启动/检查", Color.FromArgb(18, 112, 94), Color.White);
            _retryButton.Click += async delegate { await StartAsync(); };
            _openButton = MakeButton("打开内容智能体", Color.White, Color.FromArgb(18, 112, 94));
            _openButton.Enabled = false;
            _openButton.Click += delegate { OpenUrl(PageUrl); };
            _logsButton = MakeButton("打开日志目录", Color.White, Color.FromArgb(67, 81, 77));
            _logsButton.Enabled = false;
            _logsButton.Click += delegate
            {
                if (!string.IsNullOrEmpty(_logRoot) && Directory.Exists(_logRoot))
                    Process.Start(new ProcessStartInfo(_logRoot) { UseShellExecute = true });
            };
            _stopButton = MakeButton("停止服务", Color.White, Color.FromArgb(190, 45, 45));
            _stopButton.Click += async delegate { await OnStopClicked(); };
            buttons.Controls.Add(_retryButton);
            buttons.Controls.Add(_openButton);
            buttons.Controls.Add(_stopButton);
            buttons.Controls.Add(_logsButton);
            root.Controls.Add(buttons, 0, 4);

            Label note = new Label();
            note.AutoSize = true;
            note.Text = "说明：关闭本窗口可取消正在进行的启动。启动完成后关闭窗口不会停止已启动的服务。本启动器不启动会议纪要助手。";
            note.ForeColor = Color.FromArgb(96, 108, 104);
            root.Controls.Add(note, 0, 5);

            Shown += async delegate { await StartAsync(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e)
            {
                _closing = true;
                TryStopWorker();
                if (_busy)
                {
                    e.Cancel = true;
                    Hide();
                }
            };
        }

        private static Button MakeButton(string text, Color back, Color fore)
        {
            Button button = new Button();
            button.AutoSize = true;
            button.Height = 36;
            button.Padding = new Padding(12, 0, 12, 0);
            button.Margin = new Padding(0, 0, 10, 0);
            button.Text = text;
            button.BackColor = back;
            button.ForeColor = fore;
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderColor = Color.FromArgb(18, 112, 94);
            return button;
        }

        private async Task OnStopClicked()
        {
            if (_busy)
            {
                AppendLog("正在取消当前启动…");
                TryStopWorker();
                return;
            }
            await StopAsync();
        }

        private async Task StartAsync()
        {
            if (_busy || _closing) return;
            _busy = true;
            _cancelRequested = false;
            _retryButton.Enabled = false;
            _openButton.Enabled = false;
            _stopButton.Enabled = true;
            _stopButton.Text = "取消启动";
            SetStatus("正在启动", "正在检查项目文件并启动内容服务。窗口可以关闭，也可以点「取消启动」。", Color.FromArgb(45, 55, 52));
            _progress.Style = ProgressBarStyle.Marquee;
            try
            {
                await Task.Run((Action)StartWorker);
                if (_closing) return;
                AppendLog("全部启动成功，正在打开内容智能体。");
                SetStatus("全部启动成功", "内容服务和前端已通过健康检查。", Color.FromArgb(18, 112, 94));
                SafeUi(delegate
                {
                    _progress.Style = ProgressBarStyle.Continuous;
                    _progress.Value = 100;
                    _openButton.Enabled = true;
                    _logsButton.Enabled = true;
                });
                OpenUrl(PageUrl);
            }
            catch (OperationCanceledException)
            {
                if (_closing) return;
                AppendLog("启动已取消，可以重新启动。");
                SetStatus("启动已取消", "可以点击「启动/检查」重新启动。", Color.FromArgb(45, 55, 52));
                _progress.Style = ProgressBarStyle.Continuous;
                _progress.Value = 0;
            }
            catch (Exception ex)
            {
                if (_closing) return;
                AppendLog("启动失败：" + ex.Message);
                SetStatus("启动失败", ex.Message, Color.FromArgb(190, 45, 45));
                SafeUi(delegate
                {
                    _progress.Style = ProgressBarStyle.Continuous;
                    _progress.Value = 0;
                    _logsButton.Enabled = !string.IsNullOrEmpty(_logRoot) && Directory.Exists(_logRoot);
                });
            }
            finally
            {
                _busy = false;
                if (_closing) Close();
                SafeUi(delegate
                {
                    _retryButton.Enabled = true;
                    _stopButton.Enabled = true;
                    _stopButton.Text = "停止服务";
                });
            }
        }

        private async Task StopAsync()
        {
            if (_busy || _closing) return;
            if (MessageBox.Show("停止后网页将暂时不可用，确认停止本项目的前后端服务？", Text,
                MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes) return;
            _busy = true;
            _cancelRequested = false;
            _retryButton.Enabled = false;
            _stopButton.Enabled = false;
            _openButton.Enabled = false;
            SetStatus("正在停止", "正在核对并停止本项目的服务……", Color.FromArgb(45, 55, 52));
            try
            {
                await Task.Run(() => RunScript("停止-AI个性化内容智能体.ps1", ""));
                if (_closing) return;
                SetStatus("服务已停止", "", Color.FromArgb(45, 55, 52));
            }
            catch (Exception ex)
            {
                if (_closing) return;
                AppendLog("停止失败：" + ex.Message);
                SetStatus("停止失败", ex.Message, Color.FromArgb(190, 45, 45));
            }
            finally
            {
                _progress.Style = ProgressBarStyle.Continuous;
                _progress.Value = 0;
                _busy = false;
                _retryButton.Enabled = true;
                _stopButton.Enabled = true;
                _stopButton.Text = "停止服务";
                if (_closing) Close();
            }
        }

        private void StartWorker()
        {
            RunScript("start.ps1", "-NoOpen");
        }

        private void RunScript(string scriptName, string arguments)
        {
            _projectRoot = ResolveProjectRoot();
            _logRoot = Path.Combine(_projectRoot, "launcher-logs");
            Directory.CreateDirectory(_logRoot);
            AppendLog("项目目录：" + _projectRoot);

            string script = Path.Combine(_projectRoot, "launcher", scriptName);
            if (!File.Exists(script))
                throw new FileNotFoundException("找不到启动脚本：" + script);

            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                @"System32\WindowsPowerShell\v1.0\powershell.exe");
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = powershell;
            info.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File \"" + script + "\" " + arguments;
            info.WorkingDirectory = _projectRoot;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.StandardErrorEncoding = Encoding.UTF8;
            int exitCode = ScriptRunner.Run(info, AppendLog, () => _closing || _cancelRequested, 600000);
            if (exitCode != 0)
                throw new InvalidOperationException("服务操作失败，退出码 " + exitCode + "。请查看下方日志。");
        }

        private void TryStopWorker()
        {
            _cancelRequested = true;
        }

        private string ResolveProjectRoot()
        {
            string current = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            for (int i = 0; i < 4; i++)
            {
                if (Directory.Exists(Path.Combine(current, "backend"))
                    && Directory.Exists(Path.Combine(current, "frontend"))
                    && Directory.Exists(Path.Combine(current, "launcher")))
                    return current;
                DirectoryInfo parent = Directory.GetParent(current);
                if (parent == null) break;
                current = parent.FullName;
            }
            throw new DirectoryNotFoundException("找不到内容智能体项目目录（需要同时包含 backend、frontend、launcher）。");
        }

        private void AppendLog(string message)
        {
            string line = "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + message;
            try
            {
                if (!string.IsNullOrEmpty(_logRoot))
                {
                    lock (_logLock)
                        File.AppendAllText(Path.Combine(_logRoot, "launcher.log"), line + Environment.NewLine, new UTF8Encoding(false));
                }
            }
            catch { }
            SafeUi(delegate
            {
                _logBox.AppendText(line + Environment.NewLine);
                _logBox.SelectionStart = _logBox.TextLength;
                _logBox.ScrollToCaret();
            });
        }

        private void SetStatus(string status, string detail, Color color)
        {
            SafeUi(delegate
            {
                _statusLabel.Text = status;
                _statusLabel.ForeColor = color;
                _detailLabel.Text = detail;
            });
        }

        private void SafeUi(Action action)
        {
            try
            {
                if (IsDisposed || Disposing || _closing) return;
                if (InvokeRequired) BeginInvoke(action);
                else action();
            }
            catch { }
        }

        private void OpenUrl(string url)
        {
            try
            {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                MessageBox.Show("无法打开浏览器：" + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }
}
