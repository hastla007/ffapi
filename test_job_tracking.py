#!/usr/bin/env python3
"""
Test script to verify that all endpoints create jobs and show up in Job History.
This tests the fixes for job tracking on all synchronous endpoints.
"""

import requests
import time
import json
import sys
from io import BytesIO
from PIL import Image

# Base URL for the API (adjust if needed)
BASE_URL = "http://localhost:8000"

def create_test_image():
    """Create a simple test image."""
    img = Image.new('RGB', (100, 100), color='red')
    img_bytes = BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    return img_bytes

def test_endpoint(endpoint_name, test_func):
    """Test a single endpoint and verify job tracking."""
    print(f"\n{'='*60}")
    print(f"Testing: {endpoint_name}")
    print('='*60)

    try:
        job_id, status = test_func()

        if job_id:
            print(f"✓ Job ID returned: {job_id}")

            # Check if job appears in job history
            jobs_response = requests.get(f"{BASE_URL}/jobs/{job_id}?format=json")
            if jobs_response.status_code == 200:
                job_data = jobs_response.json()
                print(f"✓ Job found in Job History")
                print(f"  - Status: {job_data.get('status')}")
                print(f"  - Progress: {job_data.get('progress')}%")
                print(f"  - Message: {job_data.get('message')}")

                # Check for FFmpeg live data
                if 'fps' in job_data or 'speed' in job_data or 'frame' in job_data:
                    print(f"✓ FFmpeg Live Data present:")
                    if 'fps' in job_data:
                        print(f"  - FPS: {job_data.get('fps')}")
                    if 'speed' in job_data:
                        print(f"  - Speed: {job_data.get('speed')}")
                    if 'frame' in job_data:
                        print(f"  - Frame: {job_data.get('frame')}")
                    if 'bitrate' in job_data:
                        print(f"  - Bitrate: {job_data.get('bitrate')}")
                else:
                    print(f"ℹ FFmpeg Live Data: Not available (may appear during processing)")

                return True
            else:
                print(f"✗ Job NOT found in Job History (status: {jobs_response.status_code})")
                return False
        else:
            print(f"✗ No job_id returned")
            return False

    except Exception as e:
        print(f"✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_image_to_mp4_loop():
    """Test /image/to-mp4-loop endpoint."""
    img_bytes = create_test_image()
    files = {'file': ('test.png', img_bytes, 'image/png')}
    data = {'duration': 5, 'as_json': 'true'}

    response = requests.post(f"{BASE_URL}/image/to-mp4-loop", files=files, data=data)

    if response.status_code == 200:
        result = response.json()
        job_id = result.get('job_id')
        return job_id, "success"
    else:
        print(f"Request failed: {response.status_code}")
        print(f"Response: {response.text}")
        return None, "failed"

def test_compose_from_urls():
    """Test /compose/from-urls endpoint."""
    # Using a test video URL (you may need to adjust this)
    payload = {
        "video_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
        "width": 640,
        "height": 360,
        "fps": 30,
        "duration_ms": 3000
    }

    response = requests.post(
        f"{BASE_URL}/compose/from-urls?as_json=true",
        json=payload,
        headers={'Content-Type': 'application/json'}
    )

    if response.status_code == 200:
        result = response.json()
        job_id = result.get('job_id')
        return job_id, "success"
    else:
        print(f"Request failed: {response.status_code}")
        print(f"Response: {response.text}")
        return None, "failed"

def test_v2_run_ffmpeg_job():
    """Test /v2/run-ffmpeg-job endpoint."""
    # Simple test job: create a 3-second black video
    payload = {
        "task": {
            "inputs": [
                {"file_path": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"}
            ],
            "filter_complex": "[0:v]scale=640:360[vout]",
            "outputs": [
                {
                    "file": "output.mp4",
                    "maps": ["[vout]"],
                    "options": ["-c:v", "libx264", "-t", "3", "-an"]
                }
            ]
        }
    }

    response = requests.post(
        f"{BASE_URL}/v2/run-ffmpeg-job?as_json=true",
        json=payload,
        headers={'Content-Type': 'application/json'}
    )

    if response.status_code == 200:
        result = response.json()
        job_id = result.get('job_id')
        return job_id, "success"
    else:
        print(f"Request failed: {response.status_code}")
        print(f"Response: {response.text}")
        return None, "failed"

def main():
    print("Job Tracking Test Suite")
    print("=" * 60)
    print("Testing all endpoints to verify job tracking and FFmpeg Live Data")

    results = {}

    # Test each endpoint
    results['image_to_mp4_loop'] = test_endpoint(
        "POST /image/to-mp4-loop",
        test_image_to_mp4_loop
    )

    # Wait a bit between tests
    time.sleep(2)

    results['compose_from_urls'] = test_endpoint(
        "POST /compose/from-urls",
        test_compose_from_urls
    )

    time.sleep(2)

    results['v2_run_ffmpeg_job'] = test_endpoint(
        "POST /v2/run-ffmpeg-job",
        test_v2_run_ffmpeg_job
    )

    # Print summary
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print('='*60)

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {name}")

    print(f"\nTotal: {passed}/{total} tests passed")

    if passed == total:
        print("\n✓ All tests passed! Job tracking is working correctly.")
        return 0
    else:
        print(f"\n✗ {total - passed} test(s) failed.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
