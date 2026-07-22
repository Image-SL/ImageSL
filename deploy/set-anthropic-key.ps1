# ImageSL — set your Anthropic API key on the live Lightsail service (Option A).
#
# Usage (from PowerShell):
#     cd "C:\Users\sli92\Downloads\SolAuth\ImageSL\deploy"
#     .\set-anthropic-key.ps1 -Key "sk-ant-...your-key..."
#
# This deploys the ImageSL container WITH your key set as ANTHROPIC_API_KEY,
# enabling the AI vision + chat features. Your key stays on your machine.

param(
  [Parameter(Mandatory = $true)]
  [string]$Key
)

$ErrorActionPreference = "Stop"
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

$region  = "us-east-2"
$service = "imagesl"
$image   = "581586866061.dkr.ecr.us-east-2.amazonaws.com/imagesl:latest"

$deployment = @{
  serviceName = $service
  containers  = @{
    imagesl = @{
      image       = $image
      environment = @{
        ANTHROPIC_API_KEY = $Key
        IMAGESL_VERSION   = "2.0.0"
      }
      ports = @{ "8000" = "HTTP" }
    }
  }
  publicEndpoint = @{
    containerName = "imagesl"
    containerPort = 8000
    healthCheck   = @{
      path              = "/api/health"
      successCodes      = "200-299"
      intervalSeconds   = 10
      timeoutSeconds    = 5
      healthyThreshold  = 2
      unhealthyThreshold = 5
    }
  }
}

$tmp = Join-Path $env:TEMP "imagesl-deploy-withkey.json"
$deployment | ConvertTo-Json -Depth 8 | Out-File $tmp -Encoding utf8

Write-Host "Deploying ImageSL with your API key..."
aws lightsail create-container-service-deployment --region $region --cli-input-json "file://$tmp"

Remove-Item $tmp -Force
Write-Host ""
Write-Host "Deployment submitted. It takes ~2-5 min to go live."
Write-Host "Check status:  aws lightsail get-container-services --service-name imagesl --region us-east-2 --query 'containerServices[0].state'"
Write-Host "AI is on once /api/health shows ai_configured: true."
