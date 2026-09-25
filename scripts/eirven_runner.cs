using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("EIRVEN AI")]
[assembly: AssemblyDescription("EIRVEN AI launcher")]
[assembly: AssemblyCompany("EIRVEN")]
[assembly: AssemblyProduct("EIRVEN AI")]
[assembly: AssemblyVersion("2.0.0.67")]
[assembly: AssemblyFileVersion("2.0.0.67")]
[assembly: AssemblyInformationalVersion("2.0.0 r67-universal-engine")]

internal static class EirvenRunner
{
    [STAThread]
    private static int Main()
    {
        string root = Path.GetDirectoryName(Application.ExecutablePath);
        try
        {
            string python = FindPython(root);
            if (python == null || !File.Exists(Path.Combine(root, ".installed-v2.0.0-r67-universal-engine")))
            {
                string setup = Path.Combine(root, "scripts", "ensure_runtime.ps1");
                if (!File.Exists(setup)) throw new FileNotFoundException("Не найден сценарий установки Эрви.", setup);
                using (Process process = Process.Start(new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = "-NoProfile -ExecutionPolicy RemoteSigned -File " + Quote(setup),
                    WorkingDirectory = root,
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal
                }))
                {
                    process.WaitForExit();
                    if (process.ExitCode != 0) throw new InvalidOperationException("Установка Эрви остановилась с кодом " + process.ExitCode + ".");
                }
                python = FindPython(root);
            }
            if (python == null) throw new FileNotFoundException("После установки не найден Python Эрви.");
            string launcher = Path.Combine(root, "launcher.py");
            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = python,
                Arguments = Quote(launcher),
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            info.EnvironmentVariables["EIRVEN_INSTALL_ROOT"] = root;
            Process.Start(info);
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Эрви — не удалось запустить", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string FindPython(string root)
    {
        string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (File.Exists(pythonw)) return pythonw;
        string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        return File.Exists(python) ? python : null;
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
