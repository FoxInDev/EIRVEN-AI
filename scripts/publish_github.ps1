$ErrorActionPreference = "Stop"
$Remote = "https://github.com/FoxInDev/EIRVEN-AI.git"
$Version = "v1.2.2"

Write-Host "EIRVEN public release -> GitHub" -ForegroundColor Cyan
git --version

if (-not (git config --global user.name)) {
    git config --global user.name "Даниил"
}

if (-not (Test-Path -LiteralPath ".git")) { git init }
git branch -M main
if ((git remote) -contains "origin") { git remote set-url origin $Remote } else { git remote add origin $Remote }

git add .
git status --short
$changed = git diff --cached --name-only
if ($changed) {
    git commit -m "feat: publish EIRVEN 1.2.2 public release"
}
git push -u origin main

if (-not (git tag -l $Version)) {
    git tag -a $Version -m "EIRVEN 1.2.2 — public release"
}
git push origin $Version
Write-Host "Done: https://github.com/FoxInDev/EIRVEN-AI" -ForegroundColor Green
