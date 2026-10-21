# Downloads the programs tide calls into build/windows/bin:
#   libmpv-2.dll  playback (shinchiro's mpv-winbuild-cmake, x86_64 baseline)
#   ffmpeg.exe    beat maps and audio fx (BtbN's FFmpeg-Builds, GPL)
#   deno.exe      the JS runtime yt-dlp needs for YouTube
# Needs GH_TOKEN for the GitHub release lookups.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root "build/windows/bin"
$tmp = Join-Path $root "build/windows/download"
New-Item -ItemType Directory -Force -Path $bin, $tmp | Out-Null

function Get-Asset($repo, $pattern, $out) {
    $name = gh release view --repo $repo --json assets --jq ".assets[].name" |
        Where-Object { $_ -match $pattern } | Select-Object -First 1
    if (-not $name) { throw "no asset matching $pattern in $repo" }
    Write-Host "$repo -> $name"
    gh release download --repo $repo --pattern $name --output $out --clobber
}

# libmpv: the plain x86_64 build, not -v3, so older CPUs work too
Get-Asset "shinchiro/mpv-winbuild-cmake" '^mpv-dev-x86_64-\d+-git-[0-9a-f]+\.7z$' "$tmp/mpv.7z"
7z e "$tmp/mpv.7z" "-o$bin" "libmpv-2.dll" -y | Out-Null

Invoke-WebRequest -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile "$tmp/ffmpeg.zip"
7z e "$tmp/ffmpeg.zip" "-o$bin" "*/bin/ffmpeg.exe" -r -y | Out-Null

Get-Asset "denoland/deno" '^deno-x86_64-pc-windows-msvc\.zip$' "$tmp/deno.zip"
7z e "$tmp/deno.zip" "-o$bin" "deno.exe" -y | Out-Null

foreach ($f in "libmpv-2.dll", "ffmpeg.exe", "deno.exe") {
    if (-not (Test-Path (Join-Path $bin $f))) { throw "$f missing after download" }
}
Get-ChildItem $bin | Format-Table Name, Length
