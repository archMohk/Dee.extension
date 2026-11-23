# Runs a script in the IronPython 2.7 that pyRevit ships, outside Revit:
#   powershell -ExecutionPolicy Bypass -File tools/checks/run_ironpython.ps1 <script.py>
param([Parameter(Mandatory = $true)][string]$Script)

$engineDir = @($env:APPDATA, $env:ProgramFiles) |
    ForEach-Object { Join-Path $_ "pyRevit-Master\bin\netfx\engines\IPY2712PR" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $engineDir) { Write-Output "pyRevit's IronPython was not found."; exit 1 }

foreach ($name in "Microsoft.Scripting.Metadata", "Microsoft.Scripting", "Microsoft.Dynamic",
                  "IronPython", "IronPython.Modules") {
    $asm = [Reflection.Assembly]::LoadFrom("$engineDir\pyRevitLabs.$name.dll")
    if ($name -eq "IronPython") { $ironPython = $asm }
}

# The standard library is a zip inside pyRevitRunner.dll.
$lib = Join-Path ([IO.Path]::GetTempPath()) ("dee_ipy_lib_" + [Guid]::NewGuid().ToString("N"))
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$runner = [Reflection.Assembly]::LoadFrom("$engineDir\pyRevitRunner.dll")
$stream = $runner.GetManifestResourceStream("pyRevitRunner.python_2712pr_lib.zip")
$zip = New-Object IO.Compression.ZipArchive($stream)
[IO.Compression.ZipFileExtensions]::ExtractToDirectory($zip, $lib)
$zip.Dispose()

$engine = $ironPython.GetType("IronPython.Hosting.Python").GetMethod("CreateEngine", [Type[]]@()).Invoke($null, @())
$paths = $engine.GetSearchPaths()
$paths.Add($lib)
Get-ChildItem $lib -Directory | ForEach-Object { if (Test-Path "$($_.FullName)\json") { $paths.Add($_.FullName) } }
$engine.SetSearchPaths($paths)
$out = New-Object IO.MemoryStream
$engine.Runtime.IO.SetOutput($out, [Text.Encoding]::UTF8)
$engine.Runtime.IO.SetErrorOutput($out, [Text.Encoding]::UTF8)

$full = (Resolve-Path $Script).Path
$failed = $null
try {
    $scope = $engine.CreateScope()
    $scope.SetVariable("__file__", $full)
    [void]$engine.CreateScriptSourceFromFile($full).Execute($scope)
}
catch {
    $failed = $_.Exception
    while ($failed.InnerException) { $failed = $failed.InnerException }
}
Write-Output ([Text.Encoding]::UTF8.GetString($out.ToArray()))
Remove-Item $lib -Recurse -Force -ErrorAction SilentlyContinue
if ($failed) {
    Write-Output ("ERROR {0}: {1}" -f $failed.GetType().Name, $failed.Message)
    foreach ($value in $failed.Data.Values) { Write-Output ($value | Out-String) }
    exit 1
}
