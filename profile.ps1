if ($Env:TERM -ne "dumb") {
    Invoke-Expression (&starship init powershell)
}

Set-Alias -name notify -Value Ping-Pushover


Function Ping-Pushover {
    param (
        [string[]]$msg
    )
	$result = & curl --show-error --silent -o /dev/null -w '%{http_code}' `
	--form-string "token=$Env:PUSHOVER_API_TOKEN" `
	--form-string "user=$Env:PUSHOVER_USER_KEY" `
		--form-string "message=$msg" `
	$Env:NOTIFICATION_URL

	if  ((!$?) || ("$result" -ne 200)) {
        [Console]::Beep()
		Write-Error "Notify curl failed with $? and HTTP $result."
		return 10
	}
}
