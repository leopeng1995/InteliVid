import os

import cv2
import torch
import hdbscan
import numpy as np
import pandas as pd
from tqdm import tqdm
from umap import UMAP
import plotly.express as px
from pymilvus import FieldSchema, Collection, connections, CollectionSchema, DataType

from dotenv import load_dotenv
load_dotenv()

from janus_embedding import JanusEmbedder


class JanusClustering:
    def __init__(self, model_path, milvus_uri, milvus_user, milvus_password):
        # Initialize Janus embedder
        self.embedder = JanusEmbedder(model_path)
        
        # Connect to Milvus
        connections.connect(
            uri=milvus_uri,
            user=milvus_user,
            password=milvus_password
        )
        
        # Determine embedding dimension based on model
        if "Janus-Pro-1B" in model_path:
            dim = 2048
        elif "Janus-Pro-7B" in model_path:
            dim = 4096
        else:
            raise ValueError(f"Unknown model in path: {model_path}")

        # Define collection schema with timestamp field
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="timestamp", dtype=DataType.DOUBLE)
        ]
        schema = CollectionSchema(fields=fields, description="Janus feature vector collection")
        
        # Clean existing collection
        self.clean_database()
        
        # Create new collection
        self.collection = Collection(name="janus_data", schema=schema)

    def clean_database(self):
        """Clean existing Milvus collection"""
        print(f"[{pd.Timestamp.now()}] Dropping existing collection...")
        Collection("janus_data").drop()
        print(f"[{pd.Timestamp.now()}] Collection dropped successfully")

    def extract_frames(self, video_path, frame_interval=1):
        """Extract frames from video at specified interval"""
        cap = cv2.VideoCapture(video_path)
        frame_list = []
        timestamps = []
        frame_count = 0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        print(f"[{pd.Timestamp.now()}] Starting frame extraction from {video_path}")
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            if frame_count % frame_interval == 0:
                frame_list.append(frame)
                timestamp = frame_count / fps
                timestamps.append(timestamp)
                if frame_count % 100 == 0:
                    progress = (frame_count / total_frames) * 100
                    print(f"[{pd.Timestamp.now()}] Extracted {frame_count}/{total_frames} frames ({progress:.1f}%)")
            frame_count += 1
            
        cap.release()
        print(f"[{pd.Timestamp.now()}] Frame extraction complete. Extracted {len(frame_list)} frames")
        return frame_list, timestamps

    def encode_and_store(self, frames, timestamps):
        """Encode frames and store in Milvus"""
        embeddings = []
        total_frames = len(frames)
        print(f"[{pd.Timestamp.now()}] Starting encoding of {total_frames} frames")
        
        for frame in tqdm(frames, desc="Encoding frames", unit="frame"):
            # Convert OpenCV BGR frame to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            with torch.no_grad():
                emb = self.embedder.encode_image(frame_rgb)
                emb = emb.mean(dim=1).float().cpu().numpy()  # Convert to float32 then to numpy
                embeddings.append(emb[0])
        
        print(f"[{pd.Timestamp.now()}] Starting storage in Milvus")
        for i, (frame, embedding, timestamp) in enumerate(zip(frames, embeddings, timestamps)):
            self.collection.insert({
                "embedding": embedding,
                "timestamp": timestamp
            })
            if (i + 1) % 10 == 0:
                progress = ((i + 1) / total_frames) * 100
                print(f"[{pd.Timestamp.now()}] Stored {i + 1}/{total_frames} frames ({progress:.1f}%)")
        
        # Create index
        print(f"[{pd.Timestamp.now()}] Creating index...")
        index_params = {"index_type": "FLAT", "metric_type": "L2", "params": {}}
        self.collection.create_index(field_name="embedding", index_params=index_params)
        self.collection.flush()
        print(f"[{pd.Timestamp.now()}] Encoding and storage complete")

    def cluster(self, min_samples=3, min_cluster_size=24):
        """Perform HDBSCAN clustering using precomputed distances from Milvus"""
        print(f"[{pd.Timestamp.now()}] Starting clustering process")
        self.collection.load()
        
        # Retrieve embeddings and IDs
        print(f"[{pd.Timestamp.now()}] Retrieving embeddings and computing distances...")
        iterator = self.collection.query_iterator(
            batch_size=10, 
            expr="id > 0", 
            output_fields=["id", "embedding"]
        )
        
        ids = []
        dist = {}
        embeddings = []
        search_params = {
            "metric_type": "L2",
            "params": {"nprobe": 10}
        }

        while True:
            batch = iterator.next()
            if len(batch) == 0:
                break
                
            batch_ids = [data["id"] for data in batch]
            ids.extend(batch_ids)
            
            query_vectors = [data["embedding"] for data in batch]
            embeddings.extend(query_vectors)
            
            # Search for nearest neighbors for each vector
            results = self.collection.search(
                data=query_vectors,
                limit=50,  # Number of nearest neighbors to consider
                anns_field="embedding",
                param=search_params,
                output_fields=["id"]
            )
            
            for i, batch_id in enumerate(batch_ids):
                dist[batch_id] = []
                for result in results[i]:
                    dist[batch_id].append((result.id, result.distance))
            
            if len(embeddings) % 100 == 0:
                print(f"[{pd.Timestamp.now()}] Processed {len(embeddings)} embeddings")
        
        # Create distance matrix
        print(f"[{pd.Timestamp.now()}] Creating distance matrix...")
        ids2index = {id: idx for idx, id in enumerate(ids)}
        dist_matrix = np.full((len(ids), len(ids)), np.inf, dtype=np.float64)
        
        for id in dist:
            for result in dist[id]:
                dist_matrix[ids2index[id]][ids2index[result[0]]] = result[1]
        
        # Run HDBSCAN with precomputed distances
        print(f"[{pd.Timestamp.now()}] Running HDBSCAN clustering...")
        clusterer = hdbscan.HDBSCAN(
            min_samples=min_samples,
            min_cluster_size=min_cluster_size,
            metric='precomputed'
        )
        labels = clusterer.fit_predict(dist_matrix)
        
        print(f"[{pd.Timestamp.now()}] Clustering complete. Found {len(set(labels)) - 1} clusters")
        return labels, np.array(embeddings)

    def visualize(self, labels, embeddings):
        """Visualize clusters using UMAP"""
        print(f"[{pd.Timestamp.now()}] Starting visualization process")
        
        # Reduce dimensions with UMAP
        print(f"[{pd.Timestamp.now()}] Running UMAP dimensionality reduction...")
        umap = UMAP(n_components=2, random_state=42, n_neighbors=80, min_dist=0.1)
        umap_embeddings = umap.fit_transform(embeddings)
        
        # Create DataFrame for visualization
        print(f"[{pd.Timestamp.now()}] Creating visualization DataFrame...")
        df = pd.DataFrame(umap_embeddings, columns=["x", "y"])
        df["cluster"] = labels.astype(str)
        
        df = df[df["cluster"] != "-1"]  # Remove noise points
        
        # Create plot
        fig = px.scatter(
            df, 
            x="x", 
            y="y", 
            color="cluster", 
            title="Janus Clustering Visualization"
        )
        fig.show()

def main():
    # Configuration
    model_path = "./models/Janus-Pro-7B"
    milvus_uri = os.getenv("MILVUS_URI")
    milvus_user = os.getenv("MILVUS_USER")
    milvus_password = os.getenv("MILVUS_PASSWORD")
    video_path = "./assets/scenario_01/2月13日.mp4"
    
    # Initialize and run clustering
    clusterer = JanusClustering(model_path, milvus_uri, milvus_user, milvus_password)
    # Create output directory if it doesn't exist
    output_dir = "./output/segments/"
    os.makedirs(output_dir, exist_ok=True)
    
    # Extract frames and timestamps
    frames, timestamps = clusterer.extract_frames(video_path)
    clusterer.encode_and_store(frames, timestamps)
    labels, embeddings = clusterer.cluster()
    clusterer.visualize(labels, embeddings)
    
    # Split video into segments based on clustering
    print(f"[{pd.Timestamp.now()}] Starting video segmentation")
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    
    # Group frames by cluster
    cluster_groups = {}
    for i, label in enumerate(labels):
        if label not in cluster_groups:
            cluster_groups[label] = []
        cluster_groups[label].append(i)
    
    # Create video segments for each cluster
    for label, frame_indices in cluster_groups.items():
        if label == -1:  # Skip noise
            continue
            
        # Get start and end timestamps
        start_time = timestamps[frame_indices[0]]
        end_time = timestamps[frame_indices[-1]]
        
        # Create output video path
        output_path = os.path.join(output_dir, f"segment_{label}_{start_time:.1f}-{end_time:.1f}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        
        # Write frames to output video
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if ret:
                out.write(frame)
        
        out.release()
        print(f"[{pd.Timestamp.now()}] Created segment: {output_path}")
    
    cap.release()
    print(f"[{pd.Timestamp.now()}] Video segmentation complete")


if __name__ == "__main__":
    main()
