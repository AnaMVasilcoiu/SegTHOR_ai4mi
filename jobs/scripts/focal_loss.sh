gammas=(0.5 1 2 5)
for gamma in ${gammas[@]}; do
    sbatch jobs/scripts/focal_loss_gamma_${gamma}.job
done

# weighted focal loss
sbatch jobs/scripts/weighted_focal_loss.job