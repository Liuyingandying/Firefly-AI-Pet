# Firefly AI Pet CLI wrapper for Windows.
#
# Do not declare a param() block here. PowerShell parameter binding can treat
# Codex/Claude subcommands and flags (for example `exec`, `--json`, `-p`) as
# script parameters instead of opaque CLI argv. Reading the automatic $args
# array keeps every token positional and lets us splat it unchanged to the
# target executable.

if ($args.Count -lt 1) {
    Write-Error "Missing CLI executable path."
    exit 2
}

$Executable = [string]$args[0]
$CliArgs = @()
if ($args.Count -gt 1) {
    $CliArgs = @($args[1..($args.Count - 1)])
}

& $Executable @CliArgs
exit $LASTEXITCODE
