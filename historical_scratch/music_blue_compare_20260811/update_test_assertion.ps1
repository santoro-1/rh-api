param([Parameter(Mandatory = $true)][string]$TargetPath)

$old = '        self.assertEqual(result["filtered_counts"]["auto_eligible"], 4)'
$new = @'
        self.assertEqual(result["filtered_counts"]["enabled_and_available"], 5)
        self.assertEqual(result["filtered_counts"]["auto_eligible"], 0)
'@.TrimEnd("`r", "`n")
$text = [System.IO.File]::ReadAllText($TargetPath)
$count = ([regex]::Matches($text, [regex]::Escape($old))).Count
if ($count -ne 1) {
    throw "Expected exactly one old assertion; found $count"
}
$updated = $text.Replace($old, $new)
[System.IO.File]::WriteAllText($TargetPath, $updated, [System.Text.UTF8Encoding]::new($false))
