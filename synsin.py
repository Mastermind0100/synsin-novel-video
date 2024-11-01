import quaternion
import numpy as np
import os
import cv2

os.environ['DEBUG'] = ''

import torch
import torch.nn as nn
import torchvision.transforms as transforms

from models.networks.sync_batchnorm import convert_model
from models.base_model import BaseModel

from options.options import get_model

from PIL import Image
import time

torch.backends.cudnn.enabled = True

class SynSinModel:
    def __init__(self, model_path:str) -> None:
        self.averages = []
        opts = torch.load(model_path)['opts']
        opts.render_ids = [1]
        model = get_model(opts)
        torch_devices = [int(gpu_id.strip()) for gpu_id in opts.gpu_ids.split(",")]

        if 'sync' in opts.norm_G:
            model = convert_model(model)
            model = nn.DataParallel(model, torch_devices[0:1]).cuda()
        else:
            model = nn.DataParallel(model, torch_devices[0:1]).cuda()

        #  Load the original model to be tested
        self.model_to_test = BaseModel(model, opts)
        self.model_to_test.load_state_dict(torch.load(model_path)['state_dict'])
        self.model_to_test.eval()

        print("Loaded model")

        self.transform = transforms.Compose([
            transforms.Resize((256,256)),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])
        
        theta = -0.1
        phi = 0.1

        tx = 0
        ty = -0.25
        tz = 0

        angle_degrees = 20
        angle_radians = np.deg2rad(angle_degrees)
        rotation_matrix = np.array([
            [np.cos(angle_radians), 0, np.sin(angle_radians)],
            [0, 1, 0],
            [-np.sin(angle_radians), 0, np.cos(angle_radians)]
        ])

        self.RT = torch.eye(4).unsqueeze(0)
        # RT[0,0:3,0:3] = torch.Tensor(quaternion.as_rotation_matrix(quaternion.from_rotation_vector([phi, theta, 0])))
        self.RT[0,0:3,0:3] = torch.Tensor(quaternion.as_rotation_matrix(quaternion.from_rotation_matrix(rotation_matrix)))
        self.RT[0,0:3,3] = torch.Tensor([tx, ty, tz])
        self.batch = {
            'cameras' : [{
                'K' : torch.eye(4).unsqueeze(0),
                'Kinv' : torch.eye(4).unsqueeze(0)
            }]
        }
        
    def get_pred_frame(self, frame:np.ndarray):
        im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        temp = frame.copy()
        temp = cv2.resize(temp, (256,256))
        im = self.transform(im)
        self.batch['images'] = [im.unsqueeze(0)]
        with torch.no_grad():
            pred_imgs = self.model_to_test.model.module.forward_angle(self.batch, [self.RT])
        pred_frame = pred_imgs[0].squeeze().cpu().permute(1,2,0).numpy() * 0.5 + 0.5
        pred_frame = np.array(pred_frame)
        pred_frame = cv2.normalize(pred_frame, None, 255, 0, cv2.NORM_MINMAX, cv2.CV_8U)
        pred_frame = cv2.cvtColor(pred_frame, cv2.COLOR_RGB2BGR)

        current_depth, self.averages = self.compute_depth(temp, pred_frame, self.averages.copy())
        print(f"Average Depth: {sum(self.averages)/len(self.averages)}, Current Depth: {current_depth}")

        return frame, pred_frame
    
    def run_pred_video(self, cap:cv2.VideoCapture, save_output:bool=False):
        if save_output:
            fwidth  = int(cap.get(3))
            fheight = int(cap.get(4))
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            output_filename = 'results_generated.mp4'
            res_cap = cv2.VideoWriter(output_filename, fourcc, 60.0, (fwidth, fheight))

        print("video loaded")
        frame_counter = 0
        averages = []
        start = time.time()
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            temp = frame.copy()
            temp = cv2.resize(temp, (256,256))
            im = self.transform(im)
            self.batch['images'] = [im.unsqueeze(0)]

            with torch.no_grad():
                pred_imgs = self.model_to_test.model.module.forward_angle(self.batch, [self.RT])

            pred_frame = pred_imgs[0].squeeze().cpu().permute(1,2,0).numpy() * 0.5 + 0.5
            pred_frame = np.array(pred_frame)
            pred_frame = cv2.normalize(pred_frame, None, 255, 0, cv2.NORM_MINMAX, cv2.CV_8U)
            pred_frame = cv2.cvtColor(pred_frame, cv2.COLOR_RGB2BGR)
            
            frame_counter += 1

            current_depth, averages = self.compute_depth(temp, pred_frame, averages.copy())
            print(f"Average Depth: {sum(averages)/len(averages)}, Current Depth: {current_depth}")
            cv2.imshow("original", temp)
            cv2.imshow("generated", pred_frame)
            
            if save_output:
                res_cap.write(pred_frame)

            if cv2.waitKey(1) == ord('q'):
                break
            elif cv2.waitKey(1) == ord('p'):
                cv2.waitKey(-1)

        curr = time.time() - start
        print(f"Frame Counter: {frame_counter}, Time Elapsed: {curr}, FPS: {frame_counter//curr}")
        if save_output:
            res_cap.release()
        cv2.destroyAllWindows()


    def compute_depth(self, frame1, frame2, averages):
        focal_length = 26
        baseline = 0.12
        img1 = frame1
        img2 = frame2
        gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
        stereo = cv2.StereoBM_create(numDisparities=32, blockSize=15)
        disparity = stereo.compute(gray1, gray2)
        # disp_norm = cv2.normalize(disparity, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        depth_data = (focal_length * baseline) / (disparity + 0.0001)

        total_depth = 0
        count = 0
        # print("Depth:", depth_data)
        for depthrow in depth_data:
            max_depth_in_row = max(depthrow)
            if max_depth_in_row > 0 and max_depth_in_row < baseline*5:
                total_depth += max_depth_in_row
                count += 1

        try:    
            average_depth = total_depth/count
            averages.append(average_depth)
        except:
            average_depth = sum(averages)/len(averages)
        finally:
            return average_depth, averages

    def get_bounded_frame(self, frame: np.ndarray) -> np.ndarray:
        res = frame.copy()
        gray = cv2.cvtColor(res, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 115, 255, cv2.THRESH_BINARY_INV)

        cv2.imshow('thresh', gray)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

        contours, heirarchy = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        x = y = 50000
        w = h = 0

        for contour in contours:
            x_t,y_t,w_t,h_t = cv2.boundingRect(contour)
            x = x_t if x_t < x else x
            w = w_t if w_t > w else w
            y = y_t if y_t < y else y
            h = h_t if h_t > h else h

        cv2.rectangle(res, (x,y), (x+w, y+h), (0,255,0), 5)

        return res


if __name__ == "__main__":
    model_path = 'modelcheckpoints/realestate/zbufferpts.pth'
    cap = cv2.VideoCapture('samples/tape2.mp4')
    synsin = SynSinModel(model_path)
    synsin.run_pred_video(cap, False)
    cap.release()