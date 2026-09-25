using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("EIRVEN AI")]
[assembly: AssemblyDescription("Local personal AI for Windows")]
[assembly: AssemblyCompany("EIRVEN")]
[assembly: AssemblyProduct("EIRVEN AI")]
[assembly: AssemblyCopyright("Copyright © EIRVEN 2026")]
[assembly: AssemblyVersion("2.0.0.67")]
[assembly: AssemblyFileVersion("2.0.0.67")]
[assembly: AssemblyInformationalVersion("2.0.0 r67-universal-engine")]

internal static class NativeLauncher
{
    private const string Marker = ".installed-v2.0.0-r67-universal-engine";
    private const string PayloadMarker = ".payload-r67-universal-engine";
    private const string ResourceName = "EirvenPayload.zip";

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = GetInstallRoot();
            Directory.CreateDirectory(root);
            using (Mutex launcherMutex = new Mutex(false, "Local\\EIRVEN-AI-r67-direct-launcher"))
            {
                bool lockHeld = false;
                try
                {
                    try { lockHeld = launcherMutex.WaitOne(TimeSpan.FromMinutes(35)); }
                    catch (AbandonedMutexException) { lockHeld = true; }
                    if (!lockHeld)
                        throw new TimeoutException("Другая установка Эрви не завершилась за 35 минут.");

                    if (!File.Exists(Path.Combine(root, PayloadMarker)))
                    {
                        ExtractPayload(root);
                        InstallCurrentExecutable(root);
                        File.WriteAllText(Path.Combine(root, PayloadMarker), "2.0.0 r67-universal-engine\n");
                    }

                    if (HasArgument(args, "--materialize-only"))
                        return 0;

                    string python = InstalledPython(root);
                    if (!RuntimeReady(root, python))
                    {
                        int code = RunInstaller(root);
                        python = InstalledPython(root);
                        if (code != 0 && !RecoverCompletedInstall(root, python))
                            throw new InvalidOperationException("Установка Эрви остановилась с кодом " + code + ". Подробности сохранены в logs\\install.log.");
                    }

                    if (python == null)
                        throw new FileNotFoundException("После установки не найден Python Эрви.");

                    StartPythonLauncher(root, python);
                    return 0;
                }
                finally
                {
                    if (lockHeld) launcherMutex.ReleaseMutex();
                }
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(
                error.Message,
                "Эрви — не удалось запустить",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 1;
        }
    }

    private static string GetInstallRoot()
    {
        string overrideRoot = Environment.GetEnvironmentVariable("EIRVEN_INSTALL_ROOT");
        if (!String.IsNullOrWhiteSpace(overrideRoot))
            return Path.GetFullPath(Environment.ExpandEnvironmentVariables(overrideRoot.Trim()));
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "EIRVEN AI");
    }

    private static bool HasArgument(string[] args, string expected)
    {
        foreach (string value in args)
            if (String.Equals(value, expected, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    private static void ExtractPayload(string root)
    {
        Assembly assembly = Assembly.GetExecutingAssembly();
        using (Stream payload = assembly.GetManifestResourceStream(ResourceName))
        {
            if (payload == null) throw new InvalidDataException("В EXE отсутствует встроенный пакет Эрви.");
            using (ZipArchive archive = new ZipArchive(payload, ZipArchiveMode.Read, false))
            {
                string rootPrefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    string relative = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                    if (String.IsNullOrWhiteSpace(relative)) continue;
                    string destination = Path.GetFullPath(Path.Combine(root, relative));
                    if (!destination.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Недопустимый путь во встроенном пакете.");
                    if (entry.FullName.EndsWith("/", StringComparison.Ordinal))
                    {
                        Directory.CreateDirectory(destination);
                        continue;
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(destination));
                    entry.ExtractToFile(destination, true);
                }
            }
        }
    }

    private static void InstallCurrentExecutable(string root)
    {
        string current = Path.GetFullPath(Application.ExecutablePath);
        string installed = Path.GetFullPath(Path.Combine(root, "EIRVEN.exe"));
        if (String.Equals(current, installed, StringComparison.OrdinalIgnoreCase)) return;

        string pending = installed + ".new";
        File.Copy(current, pending, true);
        if (File.Exists(installed)) File.Delete(installed);
        File.Move(pending, installed);
    }

    private static string InstalledPython(string root)
    {
        string pythonw = Path.Combine(root, ".venv", "Scripts", "pythonw.exe");
        if (File.Exists(pythonw)) return pythonw;
        string python = Path.Combine(root, ".venv", "Scripts", "python.exe");
        return File.Exists(python) ? python : null;
    }

    private static bool RuntimeReady(string root, string python)
    {
        return python != null && File.Exists(Path.Combine(root, Marker));
    }

    private static bool RecoverCompletedInstall(string root, string python)
    {
        if (python == null) return false;
        string log = Path.Combine(root, "logs", "install.log");
        if (!File.Exists(log) || File.ReadAllText(log).IndexOf("[DONE] installation completed", StringComparison.Ordinal) < 0)
            return false;

        string consolePython = Path.Combine(Path.GetDirectoryName(python), "python.exe");
        if (!File.Exists(consolePython)) consolePython = python;
        ProcessStartInfo check = new ProcessStartInfo
        {
            FileName = consolePython,
            Arguments = "-c \"import eirven_ai; assert eirven_ai.__version__ == '2.0.0'\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        check.EnvironmentVariables["EIRVEN_ROOT_DIR"] = root;
        using (Process process = Process.Start(check))
        {
            if (!process.WaitForExit(60000) || process.ExitCode != 0) return false;
        }
        File.WriteAllText(Path.Combine(root, Marker), DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + Environment.NewLine);
        return true;
    }

    private static int RunInstaller(string root)
    {
        string script = Path.Combine(root, "scripts", "ensure_runtime.ps1");
        if (!File.Exists(script)) throw new FileNotFoundException("Не найден сценарий установки Эрви.", script);
        ProcessStartInfo info = new ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -ExecutionPolicy RemoteSigned -File " + Quote(script),
            WorkingDirectory = root,
            UseShellExecute = true,
            WindowStyle = ProcessWindowStyle.Normal
        };
        using (Process process = Process.Start(info))
        {
            process.WaitForExit();
            return process.ExitCode;
        }
    }

    private static void StartPythonLauncher(string root, string python)
    {
        string launcher = Path.Combine(root, "launcher.py");
        if (!File.Exists(launcher)) throw new FileNotFoundException("Не найден launcher.py Эрви.", launcher);
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
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }
}
