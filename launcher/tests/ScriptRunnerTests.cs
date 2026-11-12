using System;
using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Threading;
using YifabaoContentLauncher;

internal static class ScriptRunnerTests
{
    private static ProcessStartInfo Info(string arguments, bool redirect)
    {
        return new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, arguments)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = redirect,
            RedirectStandardError = redirect
        };
    }

    private static void Assert(bool condition, string label)
    {
        if (!condition) throw new Exception("FAIL: " + label);
        Console.WriteLine("PASS: " + label);
    }

    private static int Main(string[] args)
    {
        if (args.Length > 0)
        {
            if (args[0] == "sleep") { Thread.Sleep(20000); return 0; }
            if (args[0] == "inherit" || args[0] == "tree")
            {
                using (Process child = Process.Start(Info("sleep", false)))
                    Console.WriteLine(child.Id);
                if (args[0] == "tree") Thread.Sleep(20000);
                return 0;
            }
            Console.WriteLine("stdout");
            Console.Error.WriteLine("stderr");
            return 23;
        }

        var log = new StringBuilder();
        int code = ScriptRunner.Run(Info("exit", true), line => { lock (log) log.AppendLine(line); }, () => false, 5000);
        Assert(code == 23 && log.ToString().Contains("stdout") && log.ToString().Contains("stderr"),
            "preserves exit code and drains both output streams");
        bool cancelled = false;
        try { ScriptRunner.Run(Info("sleep", true), line => { }, () => true, 5000); }
        catch (OperationCanceledException) { cancelled = true; }
        Assert(cancelled, "cancellation before process creation is retained");

        int childId = 0;
        try
        {
            var timer = Stopwatch.StartNew();
            code = ScriptRunner.Run(Info("inherit", true), line => childId = int.Parse(line), () => false, 5000);
            Assert(code == 0 && childId != 0 && timer.ElapsedMilliseconds < 5000,
                "inherited output handles cannot block completion");
        }
        finally { StopFixture(childId); }

        foreach (bool cancel in new[] { false, true })
        {
            childId = 0;
            bool rejected = false;
            var timer = Stopwatch.StartNew();
            try
            {
                ScriptRunner.Run(Info("tree", true), line => childId = int.Parse(line),
                    () => cancel && timer.ElapsedMilliseconds > 1500, cancel ? 5000 : 1500);
            }
            catch (TimeoutException) { rejected = !cancel; }
            catch (OperationCanceledException) { rejected = cancel; }
            try
            {
                Assert(rejected && timer.ElapsedMilliseconds < 10000, cancel ? "active cancellation completes" : "hung worker times out");
                bool running = false;
                try { using (Process child = Process.GetProcessById(childId)) running = !child.HasExited; }
                catch (ArgumentException) { }
                Assert(childId != 0 && !running, "interrupted worker leaves no child process");
            }
            finally { StopFixture(childId); }
        }
        return 0;
    }

    private static void StopFixture(int id)
    {
        if (id == 0) return;
        try
        {
            using (Process process = Process.GetProcessById(id))
            {
                if (!process.HasExited) process.Kill();
                process.WaitForExit(5000);
            }
        }
        catch (ArgumentException) { }
    }
}
