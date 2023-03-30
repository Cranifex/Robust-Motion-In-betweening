import os
import pathlib

import numpy as np
import torch
import yaml
import torch.onnx
from torch.optim import Adam
from torch.utils.data import DataLoader
from tqdm import tqdm

from rmi.data.lafan1_dataset import LAFAN1Dataset
from rmi.data.utils import flip_bvh
from rmi.model.network import Decoder, Discriminator, InputEncoder, LSTMNetwork
from rmi.model.noise_injector import noise_injector
from rmi.model.positional_encoding import PositionalEncoding
from rmi.model.skeleton import (Skeleton, amass_offsets, sk_joints_to_remove,
                                sk_offsets, sk_parents, dfki_joints_to_remove, dfki_offsets, dfki_parents)
import shutil


def saveOnnx(state_encoder, target_encoder, offset_encoder, lstm, decoder, short_discriminator, long_discriminator, generator_optimizer, discriminator_optimizer):
    torch.onnx.export(state_encoder)

def train():
    # Load configuration from yaml
    #config = yaml.safe_load(open('./config/config_base.yaml', 'r').read())
    config = yaml.safe_load(open('./config/config2_base.yaml', 'r').read())


    # Set device to use
    gpu_id = config['device']['gpu_id']
    device = torch.device(f"cuda:{gpu_id}" if torch.cuda.is_available() else "cpu")

    # Prepare Directory
    exp_name = config['data']['exp_name']
    model_path = os.path.join('model_weights', exp_name)
    pathlib.Path(model_path).mkdir(parents=True, exist_ok=True)
    shutil.copy(src='config/config_base.yaml', dst=model_path+'/config.yaml')
    pathlib.Path(config['data']['processed_data_dir']).mkdir(parents=True, exist_ok=True)
    
    # Load Skeleton
    #Maybe edit here as well, also sk_...

    '''
    offset = sk_offsets if config['data']['dataset'] == 'LAFAN' else amass_offsets
    skeleton = Skeleton(offsets=offset, parents=sk_parents, device=device)
    skeleton.remove_joints(sk_joints_to_remove)
    '''
    # Edit - Niklas
    offset = dfki_offsets if config['data']['dataset'] == 'DFKI' else amass_offsets
    skeleton = Skeleton(offsets=offset, parents=dfki_parents, device=device)
    skeleton.remove_joints(dfki_joints_to_remove)
    
    # Flip, Load and preprocess data. It utilizes LAFAN1 utilities
    #if config['data']['flip_bvh']:
       # flip_bvh(config['data']['data_dir'], skip='subject5')
    if config['data']['flip_bvh']:
        flip_bvh(config['data']['data_dir'], skip='subject2')

    training_frames = config['model']['training_frames']
    lafan_dataset = LAFAN1Dataset(lafan_path=config['data']['data_dir'], processed_data_dir=config['data']['processed_data_dir'], train=True, 
                                  device=device, window=config['model']['window'], dataset=config['data']['dataset'])
    lafan_data_loader = DataLoader(lafan_dataset, batch_size=config['model']['batch_size'], shuffle=True, num_workers=config['data']['data_loader_workers'])

    pos_std = lafan_dataset.global_pos_std

    # Extract dimension from processed data
    root_v_dim = lafan_dataset.root_v_dim
    local_q_dim = lafan_dataset.local_q_dim
    contact_dim = lafan_dataset.contact_dim

    # Initializing networks
    state_in = root_v_dim + local_q_dim + contact_dim
    state_encoder = InputEncoder(input_dim=state_in)
    state_encoder.to(device)

    offset_in = root_v_dim + local_q_dim
    offset_encoder = InputEncoder(input_dim=offset_in)
    offset_encoder.to(device)

    target_in = local_q_dim
    target_encoder = InputEncoder(input_dim=target_in)
    target_encoder.to(device)

    # LSTM
    lstm_in = state_encoder.out_dim * 3
    lstm = LSTMNetwork(input_dim=lstm_in, hidden_dim=lstm_in, device=device)
    lstm.to(device)

    # Decoder
    decoder = Decoder(input_dim=lstm_in, out_dim=state_in)
    decoder.to(device)

    discriminator_in = lafan_dataset.num_joints * 3 * 2 # See Appendix
    short_discriminator = Discriminator(input_dim=discriminator_in, length=2)
    short_discriminator.to(device)
    long_discriminator = Discriminator(input_dim=discriminator_in, length=5)
    long_discriminator.to(device)

    pe = PositionalEncoding(dimension=256, max_len=training_frames, device=device)

    generator_optimizer = Adam(params=list(state_encoder.parameters()) + 
                                      list(offset_encoder.parameters()) + 
                                      list(target_encoder.parameters()) +
                                      list(lstm.parameters()) +
                                      list(decoder.parameters()),
                                lr=config['model']['learning_rate'],
                                betas=(config['model']['optim_beta1'], config['model']['optim_beta2']),
                                amsgrad=True)

    discriminator_optimizer = Adam(params=list(short_discriminator.parameters()) +
                                          list(long_discriminator.parameters()),
                                    lr=config['model']['learning_rate'],
                                    betas=(config['model']['optim_beta1'], config['model']['optim_beta2']),
                                    amsgrad=True)


    for epoch in tqdm(range(config['model']['epochs']), position=0, desc="Epoch"):

        state_encoder.train()
        offset_encoder.train()
        target_encoder.train()
        lstm.train()
        decoder.train()

        batch_pbar = tqdm(lafan_data_loader, position=1, desc="Batch")
        for sampled_batch in batch_pbar:
            loss_pos = 0
            loss_quat = 0
            loss_contact = 0
            loss_root = 0

            current_batch_size = len(sampled_batch['global_pos'])

            # state input
            local_q = sampled_batch['local_q'].to(device)
            root_v = sampled_batch['root_v'].to(device)
            contact = sampled_batch['contact'].to(device)
            # offset input
            root_p_offset = sampled_batch['root_p_offset'].to(device)
            local_q_offset = sampled_batch['local_q_offset'].to(device)
            local_q_offset = local_q_offset.view(current_batch_size, -1)
            # target input
            target = sampled_batch['q_target'].to(device)
            target = target.view(current_batch_size, -1)
            # root pos
            root_p = sampled_batch['root_p'].to(device)
            # global pos
            global_pos = sampled_batch['global_pos'].to(device)
            global_rot = sampled_batch['global_rot'].to(device)

            lstm.init_hidden(current_batch_size)

            # 3.4: target noise is sampled once per sequence
            target_noise = torch.normal(mean=0, std=config['model']['target_noise'], size=(current_batch_size, 256 * 2), device=device)

            root_pred_list = []
            local_q_pred_list = []
            contact_pred_list = []
            pos_next_list = []
            local_q_next_list = []
            root_p_next_list = []
            contact_next_list = []
            global_q_next_list = []

            for t in range(training_frames):

                if t == 0: # if initial frame
                    root_p_t = root_p[:,t+10]
                    root_v_t = root_v[:,t+10]
                    local_q_t = local_q[:,t+10]
                    local_q_t = local_q_t.view(local_q_t.size(0), -1)
                    contact_t = contact[:,t+10]
                else:
                    root_p_t = root_pred  # Be careful about dimension
                    root_v_t = root_v_pred[0]
                    local_q_t = local_q_pred[0]
                    contact_t = contact_pred

                assert root_p_offset.shape == root_p_t.shape

                # state input
                state_input = torch.cat([local_q_t, root_v_t, contact_t], -1)
                # offset input
                root_p_offset_t = root_p_offset - root_p_t
                local_q_offset_t = local_q_offset - local_q_t
                offset_input = torch.cat([root_p_offset_t, local_q_offset_t], -1)
                # target input
                target_input = target

                h_state = state_encoder(state_input)
                h_offset = offset_encoder(offset_input)
                h_target = target_encoder(target_input)
                
                # Use positional encoding
                tta = training_frames - t # (5 ~ 30) / (0 ~ 29) 
                h_state = pe(h_state, tta)
                h_offset = pe(h_offset, tta)  # (batch size, 256)
                h_target = pe(h_target, tta)  # (batch size, 256)

                offset_target = torch.cat([h_offset, h_target], dim=1)
                # Inject noise by scheduling
                noise_multiplier = noise_injector(t, length=training_frames)  # Noise injection
                prtbd_offset_target = offset_target + noise_multiplier * target_noise

                # lstm
                h_in = torch.cat([h_state, prtbd_offset_target], dim=1).unsqueeze(0)
                h_out = lstm(h_in)

                # decoder
                h_pred, contact_pred = decoder(h_out)
                local_q_v_pred = h_pred[:,:,:target_in]
                local_q_pred = local_q_v_pred + local_q_t

                local_q_pred_ = local_q_pred.view(local_q_pred.size(0), local_q_pred.size(1), -1, 4)
                local_q_pred_ = local_q_pred_ / torch.norm(local_q_pred_, dim = -1, keepdim = True)

                root_v_pred = h_pred[:,:,target_in:]
                root_pred = root_v_pred + root_p_t

                # root, q, contact prediction
                if root_pred.size(1) == 1:
                    root_pred = root_pred[0]
                else:
                    root_pred = root_pred.squeeze()
                if local_q_pred_.size(1) == 1:
                    local_q_pred_ = local_q_pred_[0]
                else:                
                    local_q_pred_ = local_q_pred_.squeeze() # (N, 22, 4)

                
                root_pred_list.append(root_pred)
                local_q_pred_list.append(local_q_pred_)

                if contact_pred.size(1) == 1:
                    contact_pred = contact_pred[0]
                else:
                    contact_pred = contact_pred.squeeze()
                contact_pred_list.append(contact_pred)

                # For loss
                pos_next_list.append(global_pos[:, t+1+10])
                global_q_next_list.append(global_rot[:, t+1+10])
                local_q_next_list.append(local_q[:,t+1+10].view(local_q.size(0), -1))
                root_p_next_list.append(root_p[:,t+1+10])
                contact_next_list.append(contact[:,t+1+10])
            
            root_pred_stack = torch.stack(root_pred_list, dim=1)
            local_q_pred_stack = torch.stack(local_q_pred_list, dim=1)
            contact_pred_stack = torch.stack(contact_pred_list, dim=1)
            pos_preds, pos_rot = skeleton.forward_kinematics_with_rotation(local_q_pred_stack, root_pred_stack)

            pos_next_stack = torch.stack(pos_next_list, dim=1)
            root_p_next_list = torch.stack(root_p_next_list, dim=1)
            local_q_next_list = torch.stack(local_q_next_list, dim=1)
            contact_next_list = torch.stack(contact_next_list, dim=1)
            rot_next_stack = torch.stack(global_q_next_list, dim=1)

            # Calculate L1 Norm
            # 3.7.3: We scale all of our losses to be approximately equal on the LaFAN1 dataset 
            # for an untrained network before tuning them with custom weights.
            loss_pos = torch.mean(torch.sum(torch.abs(pos_preds - pos_next_stack), dim=1) / pos_std) / training_frames
            loss_root = torch.mean(torch.sum(torch.abs(root_pred_stack - root_p_next_list), dim=1) / pos_std[0]) / training_frames
            loss_global_quat = torch.norm((pos_rot - rot_next_stack), dim=(2,3)).mean()
            loss_quat = torch.mean(torch.sum(torch.abs(local_q_pred_stack - local_q_next_list.reshape(current_batch_size, training_frames, lafan_dataset.num_joints, -1)), dim=1)) / training_frames
            loss_contact = torch.mean(torch.sum(torch.abs(contact_pred_stack - contact_next_list), dim=1)) / training_frames
            
            # Adversarial
            fake_gan_input = torch.cat([global_pos[:,0+10].reshape(current_batch_size, -1).unsqueeze(1), pos_preds.reshape(current_batch_size, training_frames, -1)], dim=1)
            fake_pos_input = fake_gan_input[:,:training_frames+1,:].permute(0,2,1)
            fake_v_input = torch.cat([fake_pos_input[:,:,1:] - fake_pos_input[:,:,:-1], torch.zeros_like(fake_pos_input[:,:,0:1], device=device)], -1)
            fake_input = torch.cat([fake_pos_input, fake_v_input], 1)

            real_pos_input = global_pos[:,10:training_frames+11].reshape(current_batch_size, training_frames+1, -1).permute(0,2,1)
            real_v_input = torch.cat([real_pos_input[:,:,1:] - real_pos_input[:,:,:-1], torch.zeros_like(real_pos_input[:,:,0:1], device=device)], -1)
            real_input = torch.cat([real_pos_input, real_v_input], 1)

            ## Discriminator
            discriminator_optimizer.zero_grad()

            # LSGAN Loss
            short_fake_logits = torch.mean(short_discriminator(fake_input.detach())[:,0], dim=1)
            short_real_logits = torch.mean(short_discriminator(real_input)[:,0], dim=1)
            short_d_fake_loss = torch.mean((short_fake_logits) ** 2)  
            short_d_real_loss = torch.mean((short_real_logits -  1) ** 2)
            short_d_loss = (short_d_fake_loss + short_d_real_loss) / 2.0

            long_fake_logits = torch.mean(long_discriminator(fake_input.detach())[:,0], dim=1)
            long_real_logits = torch.mean(long_discriminator(real_input)[:,0], dim=1)
            long_d_fake_loss = torch.mean((long_fake_logits) ** 2)
            long_d_real_loss = torch.mean((long_real_logits -  1) ** 2)
            long_d_loss = (long_d_fake_loss + long_d_real_loss) / 2.0

            total_d_loss = config['model']['loss_discriminator_weight'] * (long_d_loss + short_d_loss)
            total_d_loss.backward()
            discriminator_optimizer.step()

            generator_optimizer.zero_grad()

            loss_total = config['model']['loss_pos_weight'] * loss_pos + \
                         config['model']['loss_quat_weight'] * loss_quat + \
                         config['model']['loss_global_quat'] * loss_global_quat + \
                         config['model']['loss_root_weight'] * loss_root + \
                         config['model']['loss_contact_weight'] * loss_contact
            
            # Adversarial
            short_fake_logits = torch.mean(short_discriminator(fake_input)[:,0], 1)
            short_g_loss = torch.mean((short_fake_logits -  1) ** 2)
            long_fake_logits = torch.mean(long_discriminator(fake_input)[:,0], 1)
            long_g_loss = torch.mean((long_fake_logits -  1) ** 2)
            total_g_loss = config['model']['loss_generator_weight'] * (long_g_loss + short_g_loss)
            loss_total += total_g_loss

            # TOTAL LOSS
            loss_total.backward()

            # Gradient clipping for training stability
            torch.nn.utils.clip_grad_norm_(state_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(offset_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(target_encoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(lstm.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(decoder.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(short_discriminator.parameters(), 1.0)
            torch.nn.utils.clip_grad_norm_(long_discriminator.parameters(), 1.0)
            generator_optimizer.step()
            batch_pbar.set_postfix({'LOSS': np.round(loss_total.item(), decimals=3)})

        if (epoch + 1) % config['log']['weight_save_interval'] == 0:
            weight_epoch = 'trained_weight_' + str(epoch + 1)
            weight_path = os.path.join(model_path, weight_epoch)
            pathlib.Path(weight_path).mkdir(parents=True, exist_ok=True)
            torch.save(state_encoder.state_dict(), weight_path + '/state_encoder.pkl')
            torch.save(target_encoder.state_dict(), weight_path + '/target_encoder.pkl')
            torch.save(offset_encoder.state_dict(), weight_path + '/offset_encoder.pkl')
            torch.save(lstm.state_dict(), weight_path + '/lstm.pkl')
            torch.save(decoder.state_dict(), weight_path + '/decoder.pkl')
            torch.save(short_discriminator.state_dict(), weight_path + '/short_discriminator.pkl')
            torch.save(long_discriminator.state_dict(), weight_path + '/long_discriminator.pkl')
            if config['model']['save_optimizer']:
                torch.save(generator_optimizer.state_dict(), weight_path + '/generator_optimizer.pkl')
                torch.save(discriminator_optimizer.state_dict(), weight_path + '/discriminator_optimizer.pkl')
            
            state_encoder.eval()
            torch.onnx.export(state_encoder,
                               state_input,
                                weight_path + "\state_encoder.onnx",
                                export_params= True,
                                opset_version=9)
            
            target_encoder.eval()
            torch.onnx.export(target_encoder, target_input, weight_path + "\\target_encoder.onnx", export_params=True, opset_version=9)
            
            offset_encoder.eval()
            torch.onnx.export(offset_encoder, offset_input, weight_path + "\offset_encoder.onnx", export_params=True, opset_version=9)
            
            #Very Error, much confusion
            lstm.eval()
            torch.onnx.export(lstm, h_in, weight_path + "\lstm.onnx",  opset_version=9)

            decoder.eval()
            torch.onnx.export(decoder, h_out, weight_path + "\decoder.onnx", export_params=True,  opset_version=9)

            short_discriminator.eval()
            torch.onnx.export(short_discriminator, real_input, weight_path + "\short_discriminator.onnx", export_params=True,  opset_version=9)

            long_discriminator.eval()
            torch.onnx.export(long_discriminator, real_input, weight_path + "\long_discriminator.onnx", export_params=True,  opset_version=9)

if __name__ == '__main__':
    train()
