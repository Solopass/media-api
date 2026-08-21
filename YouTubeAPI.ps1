# ================================
# YouTube Local API Server (Full Version)
# Requires: yt-dlp, ffmpeg
# ================================
param (
    [int]$Port = 8080,
    [string]$Token = "",
    [string]$LogFile = "YouTubeAPI.log",
    [string]$OutputDir = ".\downloads"
)

# Ensure output directory exists
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

$OutputDir = (Resolve-Path $OutputDir).Path

# Job Tracking: thread-safe dictionary to share state with background threads
$global:Jobs = [System.Collections.Concurrent.ConcurrentDictionary[string, object]]::new()

# Metrics tracking
$global:Metrics = @{
    StartTime = (Get-Date)
    TotalRequests = 0
    TotalErrors = 0
    JobsCompleted = 0
}

function Write-Log {
    param([string]$Message)
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    $logLine = "[$timestamp] $Message"
    Write-Host $logLine
    Add-Content -Path $LogFile -Value $logLine
}

function Send-JsonResponse {
    param($context, $obj, $statusCode = 200)
    $json = ($obj | ConvertTo-Json -Depth 5 -Compress)
    $buffer = [System.Text.Encoding]::UTF8.GetBytes($json)
    
    $context.Response.StatusCode = $statusCode
    $context.Response.ContentType = "application/json"
    # CORS
    $context.Response.AddHeader("Access-Control-Allow-Origin", "*")
    $context.Response.AddHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    $context.Response.AddHeader("Access-Control-Allow-Headers", "Authorization, Content-Type")
    
    try {
        $context.Response.OutputStream.Write($buffer, 0, $buffer.Length)
    } catch {} finally {
        $context.Response.Close()
    }
}

function Get-QueryParams {
    param($rawUrl)
    $uri = [System.Uri]::new("http://localhost$rawUrl")
    $params = @{}
    foreach ($key in $uri.Query.TrimStart('?').Split('&')) {
        if ($key -match "=") {
            $k,$v = $key.Split('=', 2)
            $params[$k] = [System.Web.HttpUtility]::UrlDecode($v)
        }
    }
    return $params
}

function Invoke-ApiJob {
    param($JobType, $Url, $Params)
    $jobId = [guid]::NewGuid().ToString()
    
    $jobState = [pscustomobject]@{
        Id = $jobId
        Type = $JobType
        Url = $Url
        Status = "queued"
        File = ""
        Error = ""
        StartTime = (Get-Date).ToString("o")
        EndTime = ""
    }
    
    $global:Jobs.TryAdd($jobId, $jobState) | Out-Null
    
    # Start thread job
    Start-ThreadJob -ArgumentList $jobId, $global:Jobs, $JobType, $Url, $Params, $OutputDir -ScriptBlock {
        param($jId, $sharedJobs, $jType, $jUrl, $jParams, $outDir)
        
        $state = $sharedJobs[$jId]
        $state.Status = "running"
        
        try {
            if ($jType -eq "download") {
                $format = if ($jParams.format) { $jParams.format } else { "mp4" }
                
                # Check if user requested video only (no audio)
                $fArg = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                if ($jParams.audio -eq "false") {
                    $fArg = "bestvideo[ext=mp4]/bestvideo"
                } elseif ($format -eq "mp3") { $fArg = "bestaudio" }
                elseif ($format -eq "wav") { $fArg = "bestaudio" }
                elseif ($format -eq "mkv") { $fArg = "bestvideo+bestaudio/best" }
                elseif ($format -eq "webm") { $fArg = "bestvideo[ext=webm]+bestaudio[ext=webm]/best[ext=webm]/best" }

                $outTemplate = "$outDir\%(title)s_%(id)s.$format"
                
                $ytArgs = @("-f", $fArg, "-o", $outTemplate, "--no-simulate", "--print", "after_move:filepath", $jUrl)
                if ($format -eq "mp3") { $ytArgs += @("--extract-audio", "--audio-format", "mp3") }
                if ($format -eq "wav") { $ytArgs += @("--extract-audio", "--audio-format", "wav") }
                if ($format -eq "mkv") { $ytArgs += @("--merge-output-format", "mkv") }
                
                $filePath = @(& yt-dlp @ytArgs 2>&1)
                
                if ($LASTEXITCODE -ne 0) { throw "yt-dlp failed: $($filePath -join "`n")" }
                $state.File = $filePath[-1] -replace "`r", ""
                
            } elseif ($jType -eq "clip") {
                $start = $jParams.start
                $end = $jParams.end
                $format = if ($jParams.format) { $jParams.format } else { "mp4" }
                
                $sSafe = $start -replace ':','-'
                $eSafe = $end -replace ':','-'
                $outName = "clip_${sSafe}_${eSafe}_$([guid]::NewGuid().ToString().Substring(0,8)).$format"
                $outFile = Join-Path $outDir $outName
                
                $fArg = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
                if ($format -eq "mp3") { $fArg = "bestaudio" }
                elseif ($format -eq "wav") { $fArg = "bestaudio" }
                
                $ytArgs = @("-f", $fArg, "--download-sections", "*$start-$end", "-o", $outFile, "--no-simulate", $jUrl)
                if ($format -eq "mp3") { $ytArgs += @("--extract-audio", "--audio-format", "mp3") }
                if ($format -eq "wav") { $ytArgs += @("--extract-audio", "--audio-format", "wav") }
                
                $ytOut = @(& yt-dlp @ytArgs 2>&1)
                if ($LASTEXITCODE -ne 0) { throw "yt-dlp clipping failed: $($ytOut -join "`n")" }
                $state.File = $outFile
                
            } elseif ($jType -eq "transcript") {
                # download subtitles
                $outTemplate = "$outDir\%(title)s_%(id)s"
                $ytArgs = @("--write-auto-sub", "--write-sub", "--sub-lang", "en", "--skip-download", "-o", $outTemplate, $jUrl)
                & yt-dlp @ytArgs | Out-Null
                $state.File = "Subtitles downloaded to $outDir"
            }
            
            $state.Status = "completed"
        } catch {
            $state.Status = "failed"
            $state.Error = $_.Exception.Message
        }
        $state.EndTime = (Get-Date).ToString("o")
    } | Out-Null
    
    return $jobState
}

# ----------------- SERVER START -----------------
Add-Type -AssemblyName System.Net.HttpListener
Add-Type -AssemblyName System.Web

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$Port/")

try {
    $listener.Start()
    Write-Log "YouTube API server running on http://localhost:$Port/"
    Write-Log "Output Directory: $OutputDir"
} catch {
    Write-Log "Failed to start listener on http://localhost:$Port/: $_"
    exit 1
}

while ($listener.IsListening) {
    try {
        $context = $listener.GetContext()
        $request = $context.Request
        $path = $request.Url.AbsolutePath.ToLower()
        
        $global:Metrics.TotalRequests++
        Write-Log "$($request.HttpMethod) $path $($request.RemoteEndPoint.Address)"
        
        # Auto-update yt-dlp once per day on the first request
        $updateFile = Join-Path $PSScriptRoot "last_update.txt"
        $needsUpdate = $true
        if (Test-Path $updateFile) {
            try {
                $lastUpdate = [DateTime]::Parse((Get-Content $updateFile -TotalCount 1))
                if ($lastUpdate.Date -eq (Get-Date).Date) { $needsUpdate = $false }
            } catch { }
        }
        
        if ($needsUpdate) {
            Write-Log "Performing daily yt-dlp update check..."
            try {
                & yt-dlp -U 2>&1 | ForEach-Object { Write-Log "yt-dlp: $_" }
                (Get-Date).ToString("o") | Set-Content $updateFile
            } catch {
                Write-Log "Failed to update yt-dlp: $_"
            }
        }
        
        # CORS Preflight
        if ($request.HttpMethod -eq "OPTIONS") {
            Send-JsonResponse $context @{ status = "ok" }
            continue
        }
        
        # Auth Check
        if ($Token -ne "") {
            $authHeader = $request.Headers["Authorization"]
            if (-not $authHeader -or $authHeader -ne "Bearer $Token") {
                $global:Metrics.TotalErrors++
                Send-JsonResponse $context @{ error = "Unauthorized" } 401
                continue
            }
        }

        # Parse Params/Body
        $params = Get-QueryParams $request.RawUrl
        $body = ""
        if ($request.HasEntityBody) {
            $reader = New-Object System.IO.StreamReader($request.InputStream, $request.ContentEncoding)
            $body = $reader.ReadToEnd()
            $reader.Close()
        }

        switch ($path) {
            "/status" {
                $id = $params["id"]
                if ($global:Jobs.ContainsKey($id)) {
                    Send-JsonResponse $context $global:Jobs[$id]
                } else {
                    Send-JsonResponse $context @{ error = "Job not found" } 404
                }
            }
            "/download" {
                if (-not $params["url"]) {
                    Send-JsonResponse $context @{ error = "Missing 'url' parameter" } 400
                    continue
                }
                $job = Invoke-ApiJob -JobType "download" -Url $params["url"] -Params $params
                Send-JsonResponse $context $job
            }
            "/clip" {
                if (-not $params["url"] -or -not $params["start"] -or -not $params["end"]) {
                    Send-JsonResponse $context @{ error = "Missing url, start, or end parameters" } 400
                    continue
                }
                $job = Invoke-ApiJob -JobType "clip" -Url $params["url"] -Params $params
                Send-JsonResponse $context $job
            }
            "/transcript" {
                if (-not $params["url"]) {
                    Send-JsonResponse $context @{ error = "Missing 'url' parameter" } 400
                    continue
                }
                $job = Invoke-ApiJob -JobType "transcript" -Url $params["url"] -Params $params
                Send-JsonResponse $context $job
            }
            "/batch" {
                if ($request.HttpMethod -ne "POST") {
                    Send-JsonResponse $context @{ error = "Must be POST" } 405
                    continue
                }
                try {
                    $batchItems = $body | ConvertFrom-Json
                    $results = @()
                    foreach ($item in $batchItems) {
                        $p = @{
                            url = $item.url
                            start = $item.start
                            end = $item.end
                            format = $item.format
                        }
                        if ($item.start -and $item.end) {
                            $results += Invoke-ApiJob -JobType "clip" -Url $item.url -Params $p
                        } else {
                            $results += Invoke-ApiJob -JobType "download" -Url $item.url -Params $p
                        }
                    }
                    Send-JsonResponse $context @{ jobs = $results }
                } catch {
                    Send-JsonResponse $context @{ error = "Invalid JSON body" } 400
                }
            }
            "/metrics" {
                $active = 0; $completed = 0; $failed = 0
                foreach ($kv in $global:Jobs.GetEnumerator()) {
                    if ($kv.Value.Status -in @("running","queued")) { $active++ }
                    if ($kv.Value.Status -eq "completed") { $completed++ }
                    if ($kv.Value.Status -eq "failed") { $failed++ }
                }
                
                Send-JsonResponse $context @{
                    uptime = ((Get-Date) - $global:Metrics.StartTime).ToString()
                    total_requests = $global:Metrics.TotalRequests
                    total_errors = $global:Metrics.TotalErrors
                    jobs = @{
                        active = $active
                        completed = $completed
                        failed = $failed
                        total = $global:Jobs.Count
                    }
                }
            }
            "/shutdown" {
                Send-JsonResponse $context @{ message = "Shutting down YouTube API server..." }
                Write-Log "Shutdown requested via API endpoint. Stopping listener..."
                $global:Loop = $false
                Start-ThreadJob -ScriptBlock {
                    param($l)
                    Start-Sleep -Seconds 1
                    try {
                        $l.Stop()
                        $l.Close()
                    } catch {}
                    Write-Host "Server stopped."
                } -ArgumentList $listener | Out-Null
            }
            default {
                Send-JsonResponse $context @{
                    endpoints = @(
                        "GET /status?id=GUID",
                        "GET /download?url=YOUTUBE_URL&format=mp4|mp3|wav|mkv|webm",
                        "GET /clip?url=YOUTUBE_URL&start=HH:MM:SS&end=HH:MM:SS&format=mp4|mp3",
                        "GET /transcript?url=YOUTUBE_URL",
                        "POST /batch (JSON array of {url, start, end, format})",
                        "GET /metrics"
                    )
                }
            }
        }
    } catch {
        Write-Log "Error processing request: $_"
        try {
            $context.Response.StatusCode = 500
            $context.Response.Close()
        } catch {}
    }
}
