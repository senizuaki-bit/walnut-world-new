using System;
using System.Diagnostics;
using System.Text;

public static class Program
{
    public static int Main(string[] args)
    {
        var forwarded = new StringBuilder();
        forwarded.Append('"');
        forwarded.Append(@"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js");
        forwarded.Append('"');
        foreach (var argument in args)
        {
            forwarded.Append(' ');
            forwarded.Append('"');
            forwarded.Append(argument.Replace("\"", "\\\""));
            forwarded.Append('"');
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = @"C:\Program Files\nodejs\node.exe",
            Arguments = forwarded.ToString(),
            UseShellExecute = false,
        };
        using (var process = Process.Start(startInfo))
        {
        if (process == null)
        {
            Console.Error.WriteLine("Unable to start npm through the local Node.js runtime.");
            return 1;
        }

        process.WaitForExit();
        return process.ExitCode;
        }
    }
}
