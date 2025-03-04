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
        workflow_json = json_template.replace("{{text_positive}}", prompt)
        
        # Validate JSON
        try:
            workflow = json.loads(workflow_json)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON template: {e}")
            return ""
        
        # Create a unique client ID
        client_id = str(uuid.uuid4())
        
        # Construct API endpoints
        queue_url = f"{api_url}/prompt"
        history_url = f"{api_url}/history"
        
        # Submit the prompt
        p = {"prompt": workflow, "client_id": client_id}
        logger.info(f"Sending prompt to ComfyUI API: {api_url}")
        
        response = requests.post(queue_url, json=p)
        if response.status_code != 200:
            logger.error(f"Failed to queue prompt: {response.text}")
            return ""
        
        prompt_id = response.json().get('prompt_id')
        if not prompt_id:
            logger.error("No prompt_id returned")
            return ""
        
        logger.info(f"Prompt queued with ID: {prompt_id}")
        
        # Wait for the image to be generated
        output_image_path = ""
        max_attempts = 60  # Wait for maximum 60 seconds
        for attempt in range(max_attempts):
            time.sleep(1)
            
            # Check history for our prompt
            history_response = requests.get(f"{history_url}/{prompt_id}")
            if history_response.status_code != 200:
                continue
            
            history_data = history_response.json()
            if not history_data:
                continue
                
            # Extract the image data
            outputs = history_data.get('outputs', {})
            if not outputs:
                continue
                
            # Find the first output node with an image
            for node_id, node_output in outputs.items():
                if 'images' in node_output:
                    for img_data in node_output['images']:
                        # Get image data
                        filename = img_data.get('filename')
                        if not filename:
                            continue
                            
                        # Construct image URL
                        img_url = f"{api_url.replace('/api', '')}/view?filename={filename}&type=output"
                        
                        # Download the image
                        img_response = requests.get(img_url)
                        if img_response.status_code != 200:
                            logger.error(f"Failed to download image: {img_response.text}")
                            continue
                        
                        # Save the image
                        os.makedirs(output_dir, exist_ok=True)
                        image_path = os.path.join(output_dir, f"comfyui_{prompt_id}.png")
                        
                        with open(image_path, 'wb') as f:
                            f.write(img_response.content)
                        
                        logger.success(f"Image generated: {image_path}")
                        output_image_path = image_path
                        break
                
                if output_image_path:
                    break
            
            if output_image_path:
                break
        
        if not output_image_path:
            logger.error("Timed out waiting for image generation")
            return ""
            
        return output_image_path
        
    except Exception as e:
        logger.error(f"Error generating image with ComfyUI: {str(e)}")
        return ""


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