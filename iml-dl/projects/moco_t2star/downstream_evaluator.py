import logging
import os.path
import numpy as np
import torch.nn
import wandb
from dl_utils import *
from mr_utils.parameter_fitting import T2StarFit
from scipy.ndimage import binary_erosion
from core.DownstreamEvaluator import DownstreamEvaluator
from projects.moco_t2star.utils import *


class PDownstreamEvaluator(DownstreamEvaluator):
    """Downstream Tasks"""

    def __init__(self,
                 name,
                 model,
                 hyper_setup,
                 device,
                 test_data_dict,
                 checkpoint_path,
                 task="moco",
                 include_brainmask=False,
                 save_predictions=False,
                 recon_model_params=None,
                 keep_center=False):
        super(PDownstreamEvaluator, self).__init__(
            name, model, hyper_setup, device, test_data_dict, checkpoint_path
        )

        self.name = name
        self.task = task
        self.include_brainmask = include_brainmask
        self.save_predictions = save_predictions
        self.keep_center = keep_center

        if recon_model_params is not None:
            self.recon_model = load_recon_model(
                recon_model_params, self.device
            )
        else:
            logging.info("[DownstreamEvaluator::ERROR]: No reconstruction "
                         "model specified.")

    def start_task(self, global_model):
        """Function to perform analysis after training is finished."""

        if self.task == "moco":
            self.test_moco(global_model)
        else:
            logging.info("[DownstreamEvaluator::ERROR]: This task is not "
                         "implemented.")


    def test_moco(self, global_model):
        """Evaluation of motion correction downstream task."""

        logging.info("################ MoCo test #################")
        self.model.load_state_dict(global_model)
        self.model.eval()

        # Different metrics:
        keys = ["t2s-MAE", "t2s-SSIM", "img-SSIM", "img-PSNR"]
        metrics = {}
        for label in ["discrete", "continuous", "timing-experiment"]:
            metrics[label] = {}
            for descr in ["uncorr", "hrqrcorr", "phimo"]:
                metrics[label][descr] = {k: [] for k in keys}
            metrics[label]["phimo-excl-echoes"] = {k: []
                                                   for k in keys if "t2s" in k}
        logging.info("Metrics calculated within motion brainmask (no CSF). "
                     "Proper analysis to be done on motion-free brainmask!")

        if len(self.test_data_dict.keys()) > 1:
            logging.info("[DownstreamEvaluator::ERROR]: More than one "
                         "dataset is not supported.")

        dataset_key = list(self.test_data_dict.keys())[0]
        dataset = self.test_data_dict[dataset_key]
        logging.info('DATASET: {}'.format(dataset_key))

        for idx, data in enumerate(dataset):
            with torch.no_grad():
                (img_cc_fs, sens_maps, img_cc_fs_gt, img_hrqrmoco, brain_mask,
                 brain_mask_noCSF, filename, slice_num,
                 simulated_data, mask_simulation) = process_input_data(
                    self.device, data
                )

                # Forward Pass
                mask = self.model(slice_num)
                if self.save_predictions:
                    self._save_predictions(filename, mask, slice_num)

                masks = [mask]
                labels = ["continuous"]

                if not simulated_data:
                    for sub in ["SQ-struct-00", "SQ-struct-44", "SQ-struct-47",
                                "SQ-struct-48", "SQ-struct-01", "SQ-struct-02",
                                "SQ-struct-03", "SQ-struct-04",
                                "SQ-struct-04-p1-low", "SQ-struct-04-p1-mid",
                                "SQ-struct-04-p1-high", "SQ-struct-04-p2-low",
                                "SQ-struct-04-p2-mid", "SQ-struct-04-p2-high",
                                "SQ-struct-04-p3-low", "SQ-struct-04-p3-mid",
                                "SQ-struct-04-p3-high", "SQ-struct-04-p4-low",
                                "SQ-struct-04-p4-mid", "SQ-struct-04-p4-high"]:
                        if sub + "_" in filename[0]:
                            logging.info("Loading the mask from the motion timing "
                                         "experiment for comparison.")
                            tmp = np.loadtxt(
                                f"/home/iml/hannah.eichhorn/Data/mqBOLD/"
                                f"RawYoungHealthyVol/motion_timing/"
                                f"{sub}/mask.txt",
                                unpack=True).T
                            # shift to match the correct timing:
                            tmp = np.roll(tmp, 3, axis=1)
                            tmp[:, 0:3] = 1
                            tmp = torch.tensor(tmp,
                                               dtype=torch.float32).to(self.device)
                            mask_timing = torch.index_select(
                                tmp, 0, slice_num.long().flatten()
                            )
                            masks.append(mask_timing)
                            labels.append("timing-experiment")
                else:
                    logging.info("Showing the mask from the simulation for "
                                 "comparison.")


                for mask_predicted, label in zip(masks, labels):
                    prediction, img_zf, mask_full = perform_reconstruction(
                        img_cc_fs, sens_maps, mask_predicted,
                        self.recon_model, keep_central_point=self.keep_center
                    )

                    calc_t2star = T2StarFit(dim=4)
                    t2star_fs = detach_torch(calc_t2star(img_cc_fs,
                                                         mask=brain_mask))
                    t2star_fs_gt = detach_torch(calc_t2star(img_cc_fs_gt,
                                                            mask=brain_mask))
                    t2star_pred = detach_torch(calc_t2star(prediction,
                                                           mask=brain_mask))
                    if not simulated_data:
                        t2star_hrqrmoco = detach_torch(calc_t2star(img_hrqrmoco,
                                                                   mask=brain_mask))

                    # depending on exclusion rate of predicted mask,
                    # leave out the last n echoes:
                    exclude_last_echoes = determine_echoes_to_exclude(
                        mask_predicted
                    )
                    if exclude_last_echoes is not None:
                        calc_t2star = T2StarFit(
                            dim=4, exclude_last_echoes=exclude_last_echoes
                        )
                        t2star_pred_exl_echoes = detach_torch(
                            calc_t2star(prediction, mask=brain_mask)
                        )

                    for slice in [10, 11, 15, 16, 20, 21, 25, 30]:
                        if slice in slice_num:
                            ind = np.where(detach_torch(slice_num) == slice)[0][0]

                            hr_qr_example = (
                                img_hrqrmoco[ind] if not simulated_data
                                else None
                            )

                            second_mask_example = (mask_simulation[ind]
                                                   if simulated_data else None)

                            log_images_to_wandb(
                                prediction_example=prediction[ind],
                                ground_truth_example=img_cc_fs_gt[ind],
                                motion_example=img_cc_fs[ind],
                                mask_example=mask_full[ind],
                                second_mask_example=second_mask_example,
                                hr_qr_example=hr_qr_example,
                                wandb_log_name="Downstream_Example_Images_no-"
                                               "reg_{}-mask/".format(label),
                                slice=slice,
                                data_types=["magn"]
                            )

                            t2star_hrqr_example = (
                                t2star_hrqrmoco[ind] if not simulated_data
                                else None
                            )
                            log_t2star_maps_to_wandb(
                                t2star_pred=t2star_pred[ind],
                                t2star_gt=t2star_fs_gt[ind],
                                t2star_motion=t2star_fs[ind],
                                t2star_hrqr=t2star_hrqr_example,
                                wandb_log_name="Downstream_Example_T2star_no"
                                               "-reg_{}-mask/".format(label),
                                slice=slice,
                            )

                            if exclude_last_echoes is not None:
                                log_t2star_maps_to_wandb(
                                    t2star_pred=t2star_pred_exl_echoes[ind],
                                    t2star_gt=t2star_fs_gt[ind],
                                    t2star_motion=t2star_fs[ind],
                                    t2star_hrqr=t2star_hrqr_example,
                                    wandb_log_name="Downstream_Example_T2star_"
                                                   "lastechoes_no-reg_{}-"
                                                   "mask/".format(label),
                                    slice=slice,
                                )

                    # Register to motion-free image:
                    img_cc_fs_gt_np = detach_torch(img_cc_fs_gt)
                    img_fs_reg, t2star_fs_reg = reg_data_to_gt(
                        img_cc_fs_gt_np, detach_torch(img_cc_fs), t2star_fs
                    )
                    img_pred_reg, t2star_pred_reg = reg_data_to_gt(
                        img_cc_fs_gt_np, detach_torch(prediction), t2star_pred
                    )
                    if exclude_last_echoes is not None:
                        _, t2star_pred_exl_echoes_reg = reg_data_to_gt(
                            img_cc_fs_gt_np, detach_torch(prediction),
                            t2star_pred_exl_echoes
                        )
                    if not simulated_data:
                        img_hrqrmoco_reg, t2star_hrqrmoco_reg = reg_data_to_gt(
                            img_cc_fs_gt_np, detach_torch(img_hrqrmoco),
                            t2star_hrqrmoco
                        )

                    # Calculate the metrics
                    t2stars = [t2star_fs_reg, t2star_pred_reg]
                    imgs = [img_fs_reg, img_pred_reg]
                    descrs = ["uncorr", "phimo"]
                    if not simulated_data:
                        t2stars.append(t2star_hrqrmoco_reg)
                        imgs.append(img_hrqrmoco_reg)
                        descrs.append("hrqrcorr")
                    if exclude_last_echoes is not None:
                        t2stars.append(t2star_pred_exl_echoes_reg)
                        descrs.append("phimo-excl-echoes")

                    for t2star, descr in zip(t2stars, descrs):
                        metrics[label][descr]["t2s-MAE"].append(
                            calc_masked_MAE(t2star, t2star_fs_gt,
                                            detach_torch(brain_mask_noCSF))
                        )
                        metrics[label][descr]["t2s-SSIM"].append(
                            calc_masked_SSIM_3D(t2star, t2star_fs_gt,
                                             detach_torch(brain_mask_noCSF))
                        )
                    for img, descr in zip(imgs, descrs):
                        metrics[label][descr]["img-SSIM"].append(
                            calc_masked_SSIM_4D(abs(img), abs(img_cc_fs_gt_np),
                                             detach_torch(brain_mask_noCSF))
                        )
                        metrics[label][descr]["img-PSNR"].append(
                            calc_masked_PSNR_4D(abs(img), abs(img_cc_fs_gt_np),
                                             detach_torch(brain_mask_noCSF))
                        )

        # Plot the metrics:
        labels = list(metrics.keys())
        for label in labels:
            if metrics[label]["uncorr"]["t2s-MAE"] == []:
                metrics.pop(label)
        for label in list(metrics.keys()):
            for empty_label in ["phimo-excl-echoes", "hrqrcorr"]:
                if empty_label in metrics[label].keys():
                    if metrics[label][empty_label]["t2s-MAE"] == []:
                        metrics[label].pop(empty_label)
        labels = list(metrics.keys())
        img_type = list(metrics[labels[0]].keys())
        metric_type = list(metrics[labels[0]][img_type[0]].keys())

        for label in labels:
            for descr in img_type:
                for k in metric_type:
                    if k in metrics[label][descr].keys():
                        metrics[label][descr][k] = np.concatenate(
                            metrics[label][descr][k]
                        )

        for label in labels:
            print(" #### {} mask ####".format(label))
            for m in metric_type:
                print(m)
                img_type = [t for t in list(metrics[label].keys())
                            if m in metrics[label][t].keys()]
                for t in img_type:
                    print("{}: {:.4f} +/- {:.4f}".format(
                        t, metrics[label][t][m].mean(),
                        metrics[label][t][m].std()
                    ))

        metric_label_dict = {'t2s-MAE': 'MAE [ms]', 't2s-SSIM': 'SSIM',
                             'img-SSIM': 'SSIM', 'img-PSNR': 'PSNR'}
        for label in labels:
            for m in metric_type:
                img_type = [t for t in list(metrics[label].keys())
                            if m in metrics[label][t].keys()]
                figsize = (3, 2.5)
                if len(img_type) > 3:
                    figsize = (4, 2.5)
                fig = plt.figure(figsize=figsize, dpi=150)
                positions = np.arange(0, len(img_type)).astype(float)
                metric_values = [metrics[label][t][m] for t in img_type]

                for i in range(len(metric_values) - 1):
                    plt.plot(positions[i:i + 2],
                             [metric_values[i], metric_values[i + 1]],
                             color='grey', linewidth=0.4, alpha=0.8)
                plt.violinplot(
                    metric_values,
                    positions=positions,
                    showmeans=True, showextrema=False,
                    widths=0.2
                )
                plt.xticks(positions, [t.replace("-excl-", "\n-excl-")
                                       for t in img_type])
                plt.ylabel(metric_label_dict[m])
                plt.tight_layout()

                log_key = "Downstream_Metrics_{}-mask/{}".format(label, m)
                wandb.log({log_key: wandb.Image(fig)})
                plt.close(fig)

    def _save_predictions(self, filename, mask, slice_num):
        """Save the predicted masks to a text file."""

        for fn, m, sl in zip(filename, mask, slice_num):
            start_index = fn.find("SQ-struct-")
            subj = fn[start_index:start_index + 12]
            dir = "{}/{}/predicted_masks".format(
                self.checkpoint_path, subj
            )
            if not os.path.exists(dir):
                os.makedirs(dir)
            np.savetxt(
                "{}/slice_{}.txt".format(
                    dir,
                    int(sl.detach().cpu().numpy()[0])
                ),
                m.cpu().numpy()
            )
