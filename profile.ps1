if ($Env:TERM -ne "dumb") {
    Invoke-Expression (&starship init powershell)
}
