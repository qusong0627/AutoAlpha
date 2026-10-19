param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)

$ErrorActionPreference = 'Stop'
$moduleRoots = @(
  (Join-Path $PSHOME 'Modules'),
  (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
  (Join-Path $env:USERPROFILE 'Documents\WindowsPowerShell\Modules')
)
$env:PSModulePath = ($moduleRoots | Where-Object { Test-Path -LiteralPath $_ }) -join ';'

try {
  Import-Module Microsoft.PowerShell.Security -ErrorAction Stop
  $Path = $Path.TrimEnd([char]13, [char]10)
  $item = Get-Item -LiteralPath $Path -Force
  if (-not $item.PSIsContainer) {
    throw 'Backup target is not a directory.'
  }

  $acl = Get-Acl -LiteralPath $Path
  $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
  $ownerSid = ([System.Security.Principal.NTAccount]::new($acl.Owner)).Translate(
    [System.Security.Principal.SecurityIdentifier]
  )
  if ($ownerSid.Value -ne $currentSid.Value) {
    throw 'Backup directory owner is not the current Windows user.'
  }
  if (-not $acl.AreAccessRulesProtected) {
    throw 'Backup directory inherits access rules from its parent.'
  }

  $allowed = @(
    $currentSid.Value,
    'S-1-5-18',
    'S-1-5-32-544'
  )
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne 3) {
    throw 'Backup directory must have exactly three explicit access rules.'
  }
  foreach ($rule in $rules) {
    if ($rule.IsInherited -or $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
      throw 'Backup directory contains an inherited or non-allow access rule.'
    }
    if ($allowed -notcontains $rule.IdentityReference.Value) {
      throw 'Backup directory grants access to an unexpected principal.'
    }
    if ([int]$rule.FileSystemRights -ne [int][System.Security.AccessControl.FileSystemRights]::FullControl) {
      throw 'Backup directory access rules must be FullControl only.'
    }
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    if ($rule.InheritanceFlags -ne $inheritance -or
        $rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) {
      throw 'Backup directory access rules must inherit to files and subdirectories.'
    }
  }

  [Console]::Out.WriteLine('Windows backup ACL verified.')
}
catch {
  [Console]::Error.WriteLine('Windows backup ACL verification failed: ' + $_.Exception.Message)
  exit 1
}
