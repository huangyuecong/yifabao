using System;
using System.Diagnostics;
using System.Threading.Tasks;

namespace YifabaoContentLauncher
{
    internal static class ScriptRunner
    {
        internal static int Run(ProcessStartInfo info, Action<string> log, Func<bool> cancelled, int timeoutMs)
        {
            if (cancelled()) throw new OperationCanceledException();
            using (Process process = Process.Start(info))
            {
                if (process == null) throw new InvalidOperationException("Cannot start PowerShell.");
                var outputClosed = new TaskCompletionSource<bool>();
                var errorClosed = new TaskCompletionSource<bool>();
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) outputClosed.TrySetResult(true);
                    else if (e.Data.Length > 0) log(e.Data);
                };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                {
                    if (e.Data == null) errorClosed.TrySetResult(true);
                    else if (e.Data.Length > 0) log(e.Data);
                };
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                Stopwatch timer = Stopwatch.StartNew();
                try
                {
                    while (!process.WaitForExit(200))
                    {
                        if (cancelled()) throw new OperationCanceledException();
                        if (timer.ElapsedMilliseconds >= timeoutMs)
                            throw new TimeoutException("Service operation timed out. Check launcher-logs and retry.");
                    }
                    if (cancelled()) throw new OperationCanceledException();
                    // Descendants can retain inherited pipe handles after the script exits.
                    // WaitForExit() without a timeout would then wait forever for EOF.
                    if (!Task.WaitAll(new Task[] { outputClosed.Task, errorClosed.Task }, 2000))
                    {
                        process.CancelOutputRead();
                        process.CancelErrorRead();
                    }
                    if (cancelled()) throw new OperationCanceledException();
                    return process.ExitCode;
                }
                catch
                {
                    try { KillTree(process); }
                    catch (Exception ex) { log("Worker cleanup failed: " + ex.Message); }
                    throw;
                }
            }
        }

        private static void KillTree(Process process)
        {
            if (process.HasExited) return;
            try
            {
                var info = new ProcessStartInfo("taskkill.exe", "/PID " + process.Id + " /T /F");
                info.CreateNoWindow = true;
                info.UseShellExecute = false;
                using (Process killer = Process.Start(info))
                {
                    if (killer != null && !killer.WaitForExit(5000)) killer.Kill();
                }
            }
            finally
            {
                if (!process.HasExited) process.Kill();
                process.WaitForExit(5000);
            }
        }
    }
}
