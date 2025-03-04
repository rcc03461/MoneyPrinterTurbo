import json
import os
import time
import base64
import io
from pathlib import Path
import requests
import uuid
from PIL import Image
from loguru import logger
from typing import List

from app.models.schema import MaterialInfo
from app.config import config
from app.utils import utils


def generate_image_from_comfyui(prompt: str, api_url: str, json_template: str, output_dir: str) -> str:
    """
    Generate an image using ComfyUI API
    
    Args:
        prompt: The text prompt to use for image generation
        api_url: ComfyUI API URL
        json_template: JSON template with {{text_positive}} placeholder
        output_dir: Directory to save the generated image
    
    Returns:
        Path to the generated image
    """
    try:
        # Replace placeholder with actual prompt
        if "{{text_positive}}" not in json_template:
            logger.warning("Template doesn't contain {{text_positive}} placeholder. Looking for prompt in text fields...")
            # Try to parse the template and find any text field to replace
            try:
                workflow = json.loads(json_template)
                prompt_replaced = False
                
                # Look for nodes that might contain prompt text fields
                for node_id, node in workflow.items():
                    if "inputs" in node and "text" in node["inputs"]:
                        logger.info(f"Found text input in node {node_id}, replacing with prompt: {prompt}")
                        node["inputs"]["text"] = prompt
                        prompt_replaced = True
                
                if prompt_replaced:
                    json_template = json.dumps(workflow)
                    logger.info("Successfully replaced prompt in template")
                else:
                    logger.warning("Could not find any text field to replace in the template. Using as-is.")
            except Exception as e:
                logger.error(f"Error trying to auto-find prompt in template: {str(e)}")
                logger.warning("Using template as-is without prompt replacement")
        else:
            # Standard replacement
            workflow_json = json_template.replace("{{text_positive}}", prompt)
            json_template = workflow_json
            logger.info(f"Replaced {{text_positive}} placeholder with prompt: {prompt}")
        
        # Validate JSON
        try:
            workflow = json.loads(json_template)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON template: {e}")
            raise ValueError(f"Invalid JSON template: {e}")
        
        # Create a unique client ID
        client_id = str(uuid.uuid4())
        
        # Construct API endpoints
        queue_url = f"{api_url}/prompt"
        history_url = f"{api_url}/history"
        
        # Submit the prompt
        p = {"prompt": workflow, "client_id": client_id}
        logger.info(f"Sending prompt to ComfyUI API: {api_url}")
        
        try:
            response = requests.post(queue_url, json=p, timeout=20)  # Increased timeout
            if response.status_code != 200:
                error_msg = f"Failed to queue prompt: {response.text}"
                logger.error(error_msg)
                raise ValueError(error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"Failed to connect to ComfyUI API: {str(e)}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Log the full response for debugging
        try:
            response_json = response.json()
            logger.info(f"Full queue response: {json.dumps(response_json)}")
            prompt_id = response_json.get('prompt_id')
        except Exception as e:
            logger.error(f"Failed to parse response JSON: {str(e)}")
            logger.info(f"Raw response text: {response.text}")
            prompt_id = response.json().get('prompt_id')
            
        if not prompt_id:
            error_msg = "No prompt_id returned from ComfyUI API"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        logger.info(f"Prompt queued with ID: {prompt_id}")
        
        # Wait for the image to be generated
        output_image_path = ""
        max_attempts = 120  # Increased to 120 seconds (2 minutes)
        for attempt in range(max_attempts):
            time.sleep(1)
            
            # Check history for our prompt
            try:
                history_response = requests.get(f"{history_url}", timeout=10)  # Changed to get all history
                if history_response.status_code != 200:
                    logger.debug(f"Attempt {attempt+1}/{max_attempts}: History not available yet, status code: {history_response.status_code}")
                    continue
            except requests.exceptions.RequestException as e:
                logger.debug(f"Attempt {attempt+1}/{max_attempts}: Request error: {str(e)}")
                continue
            
            try:
                history_data = history_response.json()
                logger.debug(f"History data keys: {list(history_data.keys() if history_data else [])}")
                
                if not history_data:
                    logger.debug(f"Attempt {attempt+1}/{max_attempts}: No history data")
                    continue
                
                # Look for our prompt_id in the history data
                prompt_data = history_data.get(prompt_id)
                if not prompt_data:
                    logger.debug(f"Attempt {attempt+1}/{max_attempts}: Our prompt ID not found in history data yet")
                    continue
                
                # Check if the prompt execution is completed
                status = prompt_data.get('status', {})
                completed = status.get('completed', False)
                if not completed:
                    status_str = status.get('status_str', 'unknown')
                    logger.debug(f"Attempt {attempt+1}/{max_attempts}: Prompt execution not completed yet, status: {status_str}")
                    continue
                
                # Extract the outputs
                outputs = prompt_data.get('outputs', {})
                if not outputs:
                    logger.debug(f"Attempt {attempt+1}/{max_attempts}: No outputs in completed prompt data")
                    continue
                
                logger.debug(f"Output nodes: {list(outputs.keys())}")
                
                # Find the first output node with an image
                for node_id, node_output in outputs.items():
                    logger.debug(f"Checking node {node_id}: {list(node_output.keys() if node_output else [])}")
                    
                    if 'images' in node_output:
                        logger.info(f"Found images in node {node_id}: {len(node_output['images'])}")
                        for img_idx, img_data in enumerate(node_output['images']):
                            # Get image data
                            filename = img_data.get('filename')
                            if not filename:
                                logger.debug(f"No filename for image {img_idx}")
                                continue
                                
                            logger.info(f"Found image filename: {filename}")
                                
                            # Construct image URL - try both paths
                            img_url = f"{api_url.replace('/api', '')}/view?filename={filename}&type=output"
                            logger.info(f"Attempting to download image from: {img_url}")
                            
                            # Download the image
                            try:
                                img_response = requests.get(img_url, timeout=15)
                                if img_response.status_code != 200:
                                    logger.error(f"Failed to download image from primary URL: {img_response.status_code}")
                                    # Try alternative URL format
                                    alt_img_url = f"{api_url.replace('/api', '')}/output/{filename}"
                                    logger.info(f"Trying alternative URL: {alt_img_url}")
                                    img_response = requests.get(alt_img_url, timeout=15)
                                    if img_response.status_code != 200:
                                        # Try one more alternative
                                        alt_img_url2 = f"{api_url.replace('/api', '')}/outputs/{filename}"
                                        logger.info(f"Trying second alternative URL: {alt_img_url2}")
                                        img_response = requests.get(alt_img_url2, timeout=15)
                                        if img_response.status_code != 200:
                                            logger.error(f"Failed to download image from all URL formats: {img_response.status_code}")
                                            continue
                            except requests.exceptions.RequestException as e:
                                logger.error(f"Failed to download image: {str(e)}")
                                continue
                            
                            # Save the image
                            os.makedirs(output_dir, exist_ok=True)
                            image_path = os.path.join(output_dir, f"comfyui_{prompt_id}.png")
                            
                            with open(image_path, 'wb') as f:
                                f.write(img_response.content)
                            
                            logger.success(f"Image generated: {image_path}")
                            output_image_path = image_path
                            return output_image_path  # Return immediately after successful download
                    
                # If we reached this point and no image was found but there are outputs,
                # the node might still be processing or has a different output format
                if outputs:
                    logger.debug(f"Outputs found but no image available yet: {list(outputs.keys())}")
            except Exception as e:
                logger.error(f"Error processing history data: {str(e)}")
                continue
        
        if not output_image_path:
            # Check if ComfyUI is still processing the image
            try:
                # Check queue status
                queue_status_url = f"{api_url}/queue"
                status_response = requests.get(queue_status_url, timeout=10)
                if status_response.status_code == 200:
                    queue_data = status_response.json()
                    running_count = queue_data.get('running_size', 0)
                    pending_count = queue_data.get('pending_size', 0)
                    
                    if running_count > 0 or pending_count > 0:
                        error_msg = f"Timed out waiting for image generation. ComfyUI is still processing: {running_count} running, {pending_count} pending"
                        logger.error(error_msg)
                        raise ValueError(error_msg)
            except Exception as e:
                logger.error(f"Error checking queue status: {str(e)}")
                
            error_msg = "Timed out waiting for image generation. The image may have been generated in ComfyUI, but couldn't be retrieved by the application."
            logger.error(error_msg)
            raise ValueError(error_msg)
            
        return output_image_path
        
    except Exception as e:
        logger.error(f"Error generating image with ComfyUI: {str(e)}")
        if "ValueError" not in str(type(e)):
            raise ValueError(f"Error generating image with ComfyUI: {str(e)}")
        raise


def search_images_comfyui(
    search_term: str,
    task_id: str,
    api_url: str,
    json_template: str,
) -> List[MaterialInfo]:
    """
    Generate images using ComfyUI for the given search term
    
    Args:
        search_term: The search term to use for image generation
        task_id: The task ID for this generation job
        api_url: ComfyUI API URL
        json_template: JSON template with {{text_positive}} placeholder
    
    Returns:
        List of MaterialInfo objects with generated images
    """
    if not api_url or not json_template:
        logger.error("ComfyUI API URL or JSON template not provided")
        return []
    
    output_dir = utils.task_dir(sub_dir=task_id)
    
    try:
        # Generate one image per search term
        image_path = generate_image_from_comfyui(
            prompt=search_term,
            api_url=api_url,
            json_template=json_template,
            output_dir=output_dir
        )
        
        if not image_path:
            return []
        
        # Create material info
        item = MaterialInfo()
        item.provider = "comfyui"
        item.url = image_path
        item.duration = 5  # Default duration for images
        
        return [item]
        
    except Exception as e:
        logger.error(f"Error searching images from ComfyUI: {str(e)}")
        return [] 